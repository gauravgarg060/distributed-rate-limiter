#include "httplib.h"
#include "json.hpp"
#include "redis_store.hpp"
#include "bucket_script.hpp"
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <thread>
using Json=nlohmann::json;
struct ApiError:std::runtime_error { int status; ApiError(int s,const std::string& m):std::runtime_error(m),status(s){} };
std::string env(const char* key,const std::string& fallback="") { const char* p=std::getenv(key);return p?p:fallback; }
int port(const char* key,int fallback) { auto s=env(key,std::to_string(fallback));size_t n;int p=std::stoi(s,&n);if(n!=s.size()||p<1||p>65535)throw std::runtime_error("Invalid port");return p; }
struct Policy { std::string id,token,rate_text; int capacity; double rate; };
bool safe_id(const std::string& s) { return !s.empty() && s.size()<=64 && std::all_of(s.begin(),s.end(),[](unsigned char c){return std::isalnum(c)||c=='-'||c=='_';}); }
bool token_equal(const std::string& a,const std::string& b) {
  if(a.size()!=b.size()) return false;
  volatile unsigned char difference=0;
  for(size_t i=0;i<a.size();++i) difference=static_cast<unsigned char>(difference | (a[i]^b[i]));
  return difference==0;
}
int main() {
 try {
  std::ifstream file(env("TENANTS_FILE",".local/tenants.json"));
  if(!file) throw std::runtime_error("Cannot open TENANTS_FILE; run scripts/start-local.py first");
  Json config; file>>config;
  if(!config.is_array() || config.empty() || config.size()>1000) throw std::runtime_error("Expected 1-1000 tenant policies");
  std::vector<Policy> policies;
  for(const auto& item:config) {
    if(!item.is_object() || item.size()!=4 || !item.contains("id") || !item.contains("token") || !item.contains("capacity") || !item.contains("refill_per_second") || !item["id"].is_string() || !item["token"].is_string() || !item["capacity"].is_number_integer() || !item["refill_per_second"].is_number()) throw std::runtime_error("Invalid tenant policy");
    const auto capacity=item["capacity"].get<long long>(); const double rate=item["refill_per_second"].get<double>();
    auto id=item["id"].get<std::string>(); auto token=item["token"].get<std::string>();
    if(!safe_id(id)||token.size()<24||token.size()>256||capacity<1||capacity>1000000||!std::isfinite(rate)||rate<0.001||rate>1000000) throw std::runtime_error("Invalid tenant policy bounds");
    for(const auto& p:policies) if(p.id==id||p.token==token) throw std::runtime_error("Duplicate tenant or token");
    policies.push_back({id,token,item["refill_per_second"].dump(),static_cast<int>(capacity),rate});
  }
  const auto instance=env("INSTANCE_ID","api-1"), prefix=env("KEY_PREFIX","rl-demo");
  if(!safe_id(instance)||!safe_id(prefix)) throw std::runtime_error("Invalid instance/prefix");
  const auto host=env("REDIS_HOST","127.0.0.1"), password=env("REDIS_PASSWORD");
  const int redis_port=port("REDIS_PORT",6379), http_port=port("PORT",8081);
  auto store=[&]() -> RedisStore& { thread_local RedisStore s(host,redis_port,password);return s; };
  sigset_t signals;sigemptyset(&signals);sigaddset(&signals,SIGINT);sigaddset(&signals,SIGTERM);pthread_sigmask(SIG_BLOCK,&signals,nullptr);
  std::atomic<unsigned long long> sequence{0},allowed{0},denied{0},errors{0};
  const auto boot=std::to_string(std::chrono::system_clock::now().time_since_epoch().count());
  httplib::Server server;
  server.new_task_queue=[] {return new httplib::ThreadPool(8,128);};
  server.set_payload_max_length(4096);server.set_read_timeout(2);server.set_write_timeout(2);server.set_keep_alive_max_count(10);
  auto reply=[](httplib::Response& res,int status,const Json& body){res.status=status;res.set_content(body.dump(),"application/json");res.set_header("Cache-Control","no-store");};
  auto authenticate=[&](const httplib::Request& req)->const Policy& {
    auto auth=req.get_header_value("Authorization");
    if(auth.compare(0,7,"Bearer ")!=0) throw ApiError(401,"Valid bearer token required");
    auto token=auth.substr(7);
    for(const auto& p:policies)if(token_equal(token,p.token))return p;
    throw ApiError(401,"Valid bearer token required");
  };
  server.Get("/healthz",[&](const auto&,auto& res){reply(res,200,{{"status","ok"},{"instance",instance}});});
  server.Get("/readyz",[&](const auto&,auto& res){try{store().ping();reply(res,200,{{"status","ready"}});}catch(...){reply(res,503,{{"status","unavailable"}});}});
  auto handle=[&](bool inspect){return [&,inspect](const httplib::Request& req,httplib::Response& res){
    auto request_id=instance+"-"+boot+"-"+std::to_string(++sequence);
    res.set_header("X-Request-ID",request_id);res.set_header("X-Instance-ID",instance);
    try {
      const auto& policy=authenticate(req);int cost=1;
      if(!req.params.empty()) throw ApiError(400,"Query parameters are not supported");
      if(inspect) {if(!req.body.empty())throw ApiError(400,"Quota inspection accepts no body");}
      else {
        auto content_type=req.get_header_value("Content-Type");auto semicolon=content_type.find(';');content_type=content_type.substr(0,semicolon);
        if(content_type!="application/json")throw ApiError(415,"Use application/json");
        auto body=Json::parse(req.body,nullptr,false);
        if(!body.is_object() || body.size()>1 || (body.size()==1&&!body.contains("cost")))throw ApiError(400,"Expected {} or an object with cost only");
        if(body.contains("cost")) {
          if(!body["cost"].is_number_integer() || body["cost"]<1 || body["cost"]>policy.capacity)throw ApiError(400,"Cost must be an integer between 1 and capacity");
          cost=body["cost"].get<int>();
        }
      }
      auto result=store().evaluate(BUCKET_SCRIPT,prefix+":{"+policy.id+"}:bucket",policy.capacity,policy.rate_text,cost,inspect);
      Json body={{"tenant_id",policy.id},{"capacity",policy.capacity},{"refill_per_second",policy.rate},{"remaining",result[1]},{"full_after_ms",result[3]},{"request_id",request_id}};
      if(!inspect){body["allowed"]=result[0]!=0;body["retry_after_ms"]=result[2];if(result[0])++allowed;else ++denied;}
      reply(res,200,body);
    } catch(const ApiError& e){if(e.status==401)res.set_header("WWW-Authenticate","Bearer");reply(res,e.status,{{"error",e.what()},{"request_id",request_id}});}
      catch(const std::exception&){++errors;reply(res,503,{{"error","Quota store unavailable or inconsistent; decision not granted"},{"request_id",request_id}});}
  };};
  server.Post("/v1/evaluate",handle(false));server.Get("/v1/quota",handle(true));
  server.Get("/metrics",[&](const httplib::Request& req,httplib::Response& res){
    try {authenticate(req);std::ostringstream s;
      s<<"# TYPE rate_limiter_decisions_total counter\nrate_limiter_decisions_total{outcome=\"allowed\"} "<<allowed.load()<<"\nrate_limiter_decisions_total{outcome=\"denied\"} "<<denied.load()<<"\n# TYPE rate_limiter_store_errors_total counter\nrate_limiter_store_errors_total "<<errors.load()<<"\n";
      res.set_content(s.str(),"text/plain; version=0.0.4");res.set_header("Cache-Control","no-store");
    }catch(const ApiError& e){reply(res,e.status,{{"error",e.what()}});}
  });
  server.set_error_handler([&](const auto&,auto& res){if(res.body.empty())reply(res,res.status,{{"error","Request rejected"}});});
  if(!server.bind_to_port(env("BIND_HOST","127.0.0.1"),http_port))throw std::runtime_error("Cannot bind HTTP port");
  std::thread shutdown([&]{int signal;sigwait(&signals,&signal);server.stop();});
  std::cout<<instance<<" listening on "<<env("BIND_HOST","127.0.0.1")<<":"<<http_port<<std::endl;
  bool ok=server.listen_after_bind();pthread_kill(shutdown.native_handle(),SIGTERM);shutdown.join();return ok?0:1;
 }catch(const std::exception&){std::cerr<<"Startup failed: check configuration, tenant policy file, and port availability\n";return 1;}
}
