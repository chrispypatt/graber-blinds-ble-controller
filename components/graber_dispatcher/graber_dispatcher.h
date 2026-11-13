#pragma once

#include "esphome/core/component.h"
#include "esphome/core/log.h"
#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/text_sensor/text_sensor.h"
#include <algorithm>
#include <array>
#include <deque>
#include <initializer_list>
#include <vector>
#include <cstdio>

namespace esphome::graber_dispatcher {

// One owner for all GATT operations on a motor. Never run native ble_write
// actions or BLE sensor nodes alongside this node on the same client.
class GraberDispatcher : public Component, public ble_client::BLEClientNode {
 public:
  void set_token(int token) { token_ = token; }
  void set_label(const std::string &label) { label_ = label; }
  void set_dual_rail(bool dual_rail) { dual_rail_ = dual_rail; }
  void set_bottom_sensor(sensor::Sensor *sensor) { positions_[0] = sensor; }
  void set_top_sensor(sensor::Sensor *sensor) { positions_[1] = sensor; }
  void set_bottom_notify_sensor(sensor::Sensor *sensor) { notify_sensors_[0] = sensor; }
  void set_top_notify_sensor(sensor::Sensor *sensor) { notify_sensors_[1] = sensor; }
  void set_battery_sensor(sensor::Sensor *sensor) { battery_ = sensor; }
  void set_status_sensor(text_sensor::TextSensor *sensor) { status_ = sensor; }
  void setup() override;
  void loop() override;
  void dump_config() override;
  void enqueue(int mode, int rail = 0, int target = 0, int top_target = 0);
  static void enqueue_bulk(int mode, std::initializer_list<GraberDispatcher *> motors);
  void request_battery();
  static void request_all_batteries();
  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                          esp_ble_gattc_cb_param_t *param) override;

 protected:
  struct Command {
    uint32_t id{0};
    uint32_t queued{0};
    uint32_t bulk_id{0};
    uint8_t mode{0}, rail{0}, target{0}, top_target{0}, attempts{0};
  };
  struct BulkOperation {
    uint32_t id{0};
    uint32_t queued{0};
    bool released{false};
    bool failed{false};
    std::vector<GraberDispatcher *> motors;
    std::vector<GraberDispatcher *> finished;
  };
  enum Handle : uint8_t { AUTH, BOTTOM_TARGET, EXECUTE, TOP_TARGET, PRESET,
                         BOTTOM_POSITION, TOP_POSITION, BATTERY, HANDLE_COUNT };
  enum class OpKind : uint8_t { NONE, WRITE, READ, SUBSCRIBE };
  struct Step { Handle handle; uint8_t value; uint16_t settle_ms; };
  enum class Phase : uint8_t { IDLE, CONNECTING, SETUP, READY, COMMAND, OBSERVE, BACKOFF, CLOSING };

  static GraberDispatcher *connecting_;
  static std::vector<GraberDispatcher *> instances_;
  static std::deque<BulkOperation> bulk_operations_;
  static uint32_t next_id_;
  static uint32_t next_bulk_id_;
  std::string label_;
  int token_{0};
  bool dual_rail_{true};
  sensor::Sensor *positions_[2]{};
  sensor::Sensor *notify_sensors_[2]{};
  sensor::Sensor *battery_{};
  text_sensor::TextSensor *status_{};
  std::deque<Command> queue_;
  Command active_{};
  bool active_valid_{false}, stop_pending_{false};
  bool ready_{false}, authenticated_{false}, intentional_close_{false};
  bool battery_pending_{false}, battery_requested_{false};
  bool delivered_{false}, observed_change_{false}, feedback_logged_{false};
  bool cache_valid_{false}, cache_in_use_{false};
  bool reboot_after_cleanup_{false};
  Phase phase_{Phase::IDLE};
  OpKind op_{OpKind::NONE};
  std::array<uint16_t, HANDLE_COUNT> handles_{};
  std::array<uint16_t, 2> cccds_{};
  std::array<uint16_t, HANDLE_COUNT> cached_handles_{};
  std::array<uint16_t, 2> cached_cccds_{};
  std::array<uint32_t, 2> samples_{};
  std::array<uint32_t, 2> sampled_at_{};
  std::array<uint32_t, 2> command_samples_{};
  std::array<float, 2> last_values_{{NAN, NAN}};
  std::vector<Step> steps_;
  uint8_t step_{0}, setup_step_{0}, read_rail_{0};
  uint16_t op_handle_{0};
  uint32_t phase_since_{0}, op_since_{0}, next_op_at_{0}, delivered_at_{0};
  uint32_t last_sample_at_{0}, last_changed_at_{0}, last_read_at_{0};
  uint32_t battery_at_{0}, connected_at_{0}, last_loop_{0}, reconnect_at_{0};
  uint32_t connect_started_at_{0}, link_open_at_{0}, setup_done_at_{0};
  uint32_t command_started_at_{0}, command_acked_at_{0};
  bool battery_has_read_{false};
  std::array<uint8_t, 3> battery_samples_{};
  uint8_t battery_sample_count_{0};
  uint32_t battery_started_at_{0};

  static bool due_(uint32_t now, uint32_t deadline) { return int32_t(now - deadline) >= 0; }
  void report_(const char *stage);
  void enqueue_(int mode, int rail, int target, int top_target, uint32_t bulk_id);
  void start_next_();
  void connect_();
  void cached_connected_();
  void remember_handles_();
  void invalidate_cache_(const char *reason);
  void discover_();
  void prepare_command_();
  void run_setup_();
  void run_command_();
  void observe_();
  void finish_(const char *result);
  void fail_(const char *reason);
  void close_();
  void disconnected_();
  void sample_(int rail, const uint8_t *value, uint16_t length);
  void mark_delivered_();
  bool write_(Handle handle, const uint8_t *value, size_t length);
  bool read_(Handle handle);
  bool subscribe_(int rail);
  void operation_ok_();
  bool desired_reached_() const;
  bool user_work_waiting_() const;
  static BulkOperation *find_bulk_(uint32_t bulk_id);
  static void try_release_bulk_(uint32_t bulk_id);
  static void abort_bulk_(uint32_t bulk_id, GraberDispatcher *failed_motor);
  static void finish_bulk_participant_(uint32_t bulk_id, GraberDispatcher *motor, bool failed);
  static void prune_bulk_operations_();
};

}  // namespace esphome::graber_dispatcher
