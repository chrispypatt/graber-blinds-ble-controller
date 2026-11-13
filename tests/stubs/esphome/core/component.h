#pragma once
#include <cstdint>
#include <cmath>
#include <string>
#include <functional>
#include <utility>
#include <vector>
#include <algorithm>
namespace esphome {
inline uint32_t test_now = 1000;
inline uint32_t millis() { return test_now; }
class Component {
 public:
  virtual ~Component() = default;
  virtual void setup() {}
  virtual void loop() {}
  virtual void dump_config() {}
  virtual float get_setup_priority() const { return 0; }
  void status_set_warning() {}
  void status_clear_warning() {}
  void mark_failed() {}
};
}
