#pragma once
namespace esphome {
class Application {
 public:
  int reboots=0;
  void safe_reboot() { ++reboots; }
};
inline Application App;
}
