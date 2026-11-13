#pragma once
#include <vector>
namespace esphome::sensor {
class Sensor {
 public:
  float state = NAN;
  std::vector<float> history;
  void publish_state(float value) { state = value; history.push_back(value); }
};
}
