#pragma once
#include <hiredis.h>

#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

// Named fields keep the HTTP layer independent of Lua's positional reply format.
struct QuotaDecision {
    bool allowed;
    long long remaining;
    long long retry_after_ms;
    long long full_after_ms;
};

// One instance belongs to one worker thread. Owns its connection and all replies.
class RedisStore {

    // Custom deleters let unique_ptr release hiredis resources on every error path.
    struct ContextDelete {
        void operator()(redisContext* p) const {
            if (p) {
                redisFree(p);
            }
        }
    };
    struct ReplyDelete {
        void operator()(redisReply* p) const {
            if (p) {
                freeReplyObject(p);
            }
        }
    };
    using Reply = std::unique_ptr<redisReply, ReplyDelete>;
    std::unique_ptr<redisContext, ContextDelete> connection_;
    std::string host_, password_;
    int port_;

    // Reuse a healthy connection. A later request reconnects after a network failure.
    void connect() {
        if (connection_ && !connection_->err) {
            return;
        }
        timeval timeout{1, 0};
        connection_.reset(redisConnectWithTimeout(host_.c_str(), port_, timeout));
        if (!connection_ || connection_->err ||
            redisSetTimeout(connection_.get(), timeout) != REDIS_OK) {
            connection_.reset();
            throw std::runtime_error("Redis unavailable");
        }
        if (!password_.empty()) {
            Reply reply(static_cast<redisReply*>(
                redisCommand(connection_.get(), "AUTH %b", password_.data(), password_.size())));
            if (!reply || reply->type == REDIS_REPLY_ERROR) {
                connection_.reset();
                throw std::runtime_error("Redis authentication failed");
            }
        }
    }

  public:
    RedisStore(std::string host, int port, std::string password)
        : host_(std::move(host)), password_(std::move(password)), port_(port) {}

    // Send separate argument buffers and lengths, rather than concatenating a command.
    Reply command(const std::vector<std::string>& args) {
        connect();
        std::vector<const char*> argv;
        std::vector<size_t> lengths;
        for (const auto& value : args) {
            argv.push_back(value.data());
            lengths.push_back(value.size());
        }
        Reply reply(static_cast<redisReply*>(redisCommandArgv(
            connection_.get(), static_cast<int>(args.size()), argv.data(), lengths.data())));
        // Never retry a possibly executed consumption after a network error.
        if (!reply) {
            connection_.reset();
            throw std::runtime_error("Redis outcome unknown");
        }
        if (reply->type == REDIS_REPLY_ERROR) {
            throw std::runtime_error("Redis operation rejected");
        }
        return reply;
    }

    // Used by /readyz; reachability does not prove every stored bucket is valid.
    void ping() {
        auto reply = command({"PING"});
        if (reply->type != REDIS_REPLY_STATUS || std::string(reply->str, reply->len) != "PONG") {
            throw std::runtime_error("Redis not ready");
        }
    }

    // Lua returns: allowed, remaining whole tokens, retry delay, time until full.
    QuotaDecision evaluate(const std::string& script, const std::string& key, int capacity,
                           const std::string& rate, int cost, bool inspect) {
        auto reply = command({"EVAL", script, "1", key, std::to_string(capacity), rate,
                              std::to_string(cost), inspect ? "inspect" : "consume"});
        if (reply->type != REDIS_REPLY_ARRAY || reply->elements != 4) {
            throw std::runtime_error("Invalid Redis result");
        }
        for (size_t i = 0; i < 4; ++i) {
            if (reply->element[i]->type != REDIS_REPLY_INTEGER) {
                throw std::runtime_error("Invalid Redis result");
            }
        }
        return {
            reply->element[0]->integer != 0,
            reply->element[1]->integer,
            reply->element[2]->integer,
            reply->element[3]->integer,
        };
    }
};
