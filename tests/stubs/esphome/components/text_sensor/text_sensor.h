#pragma once
#include <string>
#include <vector>
namespace esphome::text_sensor {
class TextSensor {
 public:
  std::string state;
  std::vector<std::string> history;
  void publish_state(const std::string &value) { state = value; history.push_back(value); }
};
}
