#pragma once
#include <hiredis.h>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

class RedisStore {
  struct ContextDelete { void operator()(redisContext* p) const { if(p) redisFree(p); } };
  struct ReplyDelete { void operator()(redisReply* p) const { if(p) freeReplyObject(p); } };
  using Reply = std::unique_ptr<redisReply, ReplyDelete>;
  std::unique_ptr<redisContext, ContextDelete> connection_;
  std::string host_, password_;
  int port_;
  void connect() {
    if(connection_ && !connection_->err) return;
    timeval timeout{1,0};
    connection_.reset(redisConnectWithTimeout(host_.c_str(),port_,timeout));
    if(!connection_ || connection_->err || redisSetTimeout(connection_.get(),timeout)!=REDIS_OK) {
      connection_.reset(); throw std::runtime_error("Redis unavailable");
    }
    if(!password_.empty()) {
      Reply reply(static_cast<redisReply*>(redisCommand(connection_.get(),"AUTH %b",password_.data(),password_.size())));
      if(!reply || reply->type==REDIS_REPLY_ERROR) { connection_.reset(); throw std::runtime_error("Redis authentication failed"); }
    }
  }
public:
  RedisStore(std::string host,int port,std::string password):host_(std::move(host)),password_(std::move(password)),port_(port) {}
  Reply command(const std::vector<std::string>& args) {
    connect();
    std::vector<const char*> argv; std::vector<size_t> lengths;
    for(const auto& value:args) { argv.push_back(value.data()); lengths.push_back(value.size()); }
    Reply reply(static_cast<redisReply*>(redisCommandArgv(connection_.get(),static_cast<int>(args.size()),argv.data(),lengths.data())));
    // Never retry a possibly executed consumption after a network error.
    if(!reply) { connection_.reset(); throw std::runtime_error("Redis outcome unknown"); }
    if(reply->type==REDIS_REPLY_ERROR) throw std::runtime_error("Redis operation rejected");
    return reply;
  }
  void ping() {
    auto reply=command({"PING"});
    if(reply->type!=REDIS_REPLY_STATUS || std::string(reply->str,reply->len)!="PONG") throw std::runtime_error("Redis not ready");
  }
  std::vector<long long> evaluate(const std::string& script,const std::string& key,
      int capacity,const std::string& rate,int cost,bool inspect) {
    auto reply=command({"EVAL",script,"1",key,std::to_string(capacity),rate,std::to_string(cost),inspect?"inspect":"consume"});
    if(reply->type!=REDIS_REPLY_ARRAY || reply->elements!=4) throw std::runtime_error("Invalid Redis result");
    std::vector<long long> result;
    for(size_t i=0;i<4;++i) {
      if(reply->element[i]->type!=REDIS_REPLY_INTEGER) throw std::runtime_error("Invalid Redis result");
      result.push_back(reply->element[i]->integer);
    }
    return result;
  }
};
