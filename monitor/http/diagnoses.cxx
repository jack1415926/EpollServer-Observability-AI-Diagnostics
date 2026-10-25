#include "diagnoses.h"
#include <drogon/HttpClient.h>
#include <charconv>
#include <cstdlib>
#include <regex>
#include <stdexcept>

namespace epoll_monitor {
DiagnosisProxy::DiagnosisProxy()
{
    const char *token = std::getenv("EPOLL_DIAG_TOKEN");
    if (!token || !*token) return;
    token_ = token;
    if (token_.size() < 32 || token_.size() > 256 ||
        token_.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~+/=") != std::string::npos)
        throw std::runtime_error("invalid_diagnosis_token");
    unsigned port = 8090;
    if (const char *raw = std::getenv("EPOLL_DIAG_PORT")) {
        const std::string text = raw;
        const auto parsed = std::from_chars(text.data(), text.data() + text.size(), port);
        if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || !port || port > 65535)
            throw std::runtime_error("invalid_diagnosis_port");
    }
    address_ = "http://127.0.0.1:" + std::to_string(port);
}

void DiagnosisProxy::Handle(const drogon::HttpRequestPtr &request, ExperimentStore::Reply reply)
{
    const std::string path = request->path();
    const bool collection = path == "/api/v1/diagnoses";
    const bool submitting = collection && request->method() == drogon::Post;
    static const std::regex detail("/api/v1/diagnoses/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}");
    if (!collection && !std::regex_match(path, detail)) { reply(400, "invalid_diagnosis_id", nullptr); return; }
    if (!request->query().empty() || (!submitting && !request->body().empty())) {
        reply(400, "unexpected_input", nullptr); return;
    }
    if (request->method() != (submitting ? drogon::Post : drogon::Get)) {
        reply(405, "method_not_allowed", nullptr); return;
    }
    if (submitting) {
        try {
            const auto value = nlohmann::json::parse(request->body());
            if (!value.is_object() || value.size() != 1 || !value.contains("question") ||
                !value["question"].is_string() || value["question"].get<std::string>().empty() ||
                value["question"].get<std::string>().size() > 4000)
                throw std::runtime_error("invalid_question");
        } catch (const std::exception &) { reply(400, "invalid_diagnosis_request", nullptr); return; }
    }
    if (address_.empty()) { reply(503, "diagnosis_disabled", nullptr); return; }
    if (pending_.fetch_add(1) >= 8) { --pending_; reply(503, "diagnosis_busy", nullptr); return; }
    const auto client = drogon::HttpClient::newHttpClient(address_);
    const auto forwarded = drogon::HttpRequest::newHttpRequest();
    forwarded->setMethod(submitting ? drogon::Post : drogon::Get);
    forwarded->setPath("/internal/diagnoses" + path.substr(std::string("/api/v1/diagnoses").size()));
    forwarded->addHeader("Authorization", "Bearer " + token_);
    if (submitting) {
        forwarded->setContentTypeCode(drogon::CT_APPLICATION_JSON);
        forwarded->setBody(std::string(request->body()));
    }
    client->sendRequest(forwarded, [this, reply](drogon::ReqResult result, const drogon::HttpResponsePtr &response) {
        --pending_;
        if (result != drogon::ReqResult::Ok || !response || response->body().size() > 2 * 1024 * 1024) {
            reply(503, "diagnosis_service_unavailable", nullptr); return;
        }
        try {
            const auto value = nlohmann::json::parse(response->body());
            const int status = static_cast<int>(response->statusCode());
            if (!value.is_object() || !value.contains("code") || !value["code"].is_string() ||
                !value.contains("data") || status < 200 || status > 599)
                throw std::runtime_error("invalid_internal_response");
            reply(status, value["code"].get<std::string>(), value["data"]);
        } catch (const std::exception &) { reply(503, "diagnosis_service_unavailable", nullptr); }
    }, 4.0);
}
}
