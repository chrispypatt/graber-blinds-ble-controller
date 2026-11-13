#pragma once
#include <cstdarg>
#include <cstdio>
#include <string>
#include <vector>
namespace test_log {
inline std::vector<std::string> messages;
inline void emit(const char *tag,const char *format,...) __attribute__((format(printf,2,3)));
inline void emit(const char *tag,const char *format,...) {
  (void)tag;
  char buffer[512];va_list arguments;va_start(arguments,format);
  vsnprintf(buffer,sizeof(buffer),format,arguments);va_end(arguments);
  messages.emplace_back(buffer);
}
}
#define ESP_LOGCONFIG(...) test_log::emit(__VA_ARGS__)
#define ESP_LOGI(...) test_log::emit(__VA_ARGS__)
#define ESP_LOGW(...) test_log::emit(__VA_ARGS__)
#define ESP_LOGE(...) test_log::emit(__VA_ARGS__)
#define ESP_LOGD(...) test_log::emit(__VA_ARGS__)
#define ESP_LOGV(...) test_log::emit(__VA_ARGS__)
