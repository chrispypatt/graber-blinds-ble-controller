#include "graber_dispatcher.h"
#include "esphome/core/hal.h"
#include "esphome/core/application.h"
#include "esphome/components/esp32_ble_client/ble_characteristic.h"
#include <cstring>

namespace esphome::graber_dispatcher {
namespace espbt = esphome::esp32_ble_tracker;
static const char *const TAG = "graber.dispatch";
static constexpr uint32_t BATTERY_INTERVAL = 6UL * 60 * 60 * 1000;
static constexpr uint32_t OP_TIMEOUT = 4000;
static constexpr uint32_t CONNECT_TIMEOUT = 15000;
static constexpr uint32_t OBSERVE_LIMIT = 90000;
static constexpr uint32_t IDLE_CONNECTION_HOLD = 30000;
GraberDispatcher *GraberDispatcher::connecting_ = nullptr;
std::vector<GraberDispatcher *> GraberDispatcher::instances_;
std::deque<GraberDispatcher::BulkOperation> GraberDispatcher::bulk_operations_;
uint32_t GraberDispatcher::next_id_ = 0;
uint32_t GraberDispatcher::next_bulk_id_ = 0;

void GraberDispatcher::setup() {
  instances_.push_back(this);
  report_("idle");
}

void GraberDispatcher::dump_config() {
  ESP_LOGCONFIG(TAG, "%s: acknowledged dispatcher, staged bulk release, phase telemetry, learned-handle fast reconnect, 3 total attempts, 30s warm hold, 90s movement limit",
                label_.c_str());
}

void GraberDispatcher::report_(const char *stage) {
  const auto now = millis();
  const uint32_t total = active_.queued ? now - active_.queued : 0;
  const uint32_t connect_ms = link_open_at_ && connect_started_at_ ? link_open_at_ - connect_started_at_ : 0;
  const uint32_t setup_ms = setup_done_at_ && link_open_at_ ? setup_done_at_ - link_open_at_ : 0;
  const uint32_t ack_ms = command_acked_at_ && command_started_at_ ? command_acked_at_ - command_started_at_ : 0;
  const uint32_t observe_ms = command_acked_at_ && now >= command_acked_at_ ? now - command_acked_at_ : 0;
  char message[240];
  snprintf(message, sizeof(message),
           "cmd=%lu bulk=%lu mode=%u attempt=%u total=%lums connect=%lums setup=%lums ack=%lums observe=%lums cache=%s %s",
           (unsigned long) active_.id, (unsigned long) active_.bulk_id, active_.mode, active_.attempts,
           (unsigned long) total, (unsigned long) connect_ms, (unsigned long) setup_ms,
           (unsigned long) ack_ms, (unsigned long) observe_ms, cache_in_use_ ? "hit" : "miss", stage);
  ESP_LOGI(TAG, "%s %s", label_.c_str(), message);
  if (status_ != nullptr) status_->publish_state(message);
}

void GraberDispatcher::enqueue(int mode, int rail, int target, int top_target) {
  enqueue_(mode, rail, target, top_target, 0);
}

void GraberDispatcher::enqueue_(int mode, int rail, int target, int top_target, uint32_t bulk_id) {
  if (mode < 0 || mode > 10 || rail < 0 || rail > 1 || target < 0 || target > 100 ||
      top_target < 0 || top_target > 100) {
    ESP_LOGW(TAG, "%s rejected invalid command", label_.c_str());
    return;
  }
  if (!dual_rail_ && (rail != 0 || mode == 6 || mode == 7 || mode == 8 || mode == 9 || mode == 10)) {
    ESP_LOGW(TAG, "%s rejected command requiring a second rail", label_.c_str());
    return;
  }
  if (mode == 8) {
    ESP_LOGW(TAG, "%s rejected paired absolute targets: rail independence is unverified", label_.c_str());
    if (status_) status_->publish_state("paired_target_unverified: command rejected");
    return;
  }
  if (mode == 2) { request_battery(); return; }
  Command next{++next_id_, millis(), bulk_id, uint8_t(mode), uint8_t(rail), uint8_t(target),
               uint8_t(top_target), 0};
  if (mode == 1) {
    std::vector<uint32_t> interrupted_bulk_ids;
    if (active_valid_ && active_.bulk_id) interrupted_bulk_ids.push_back(active_.bulk_id);
    for (const auto &waiting : queue_)
      if (waiting.bulk_id && std::find(interrupted_bulk_ids.begin(), interrupted_bulk_ids.end(), waiting.bulk_id) == interrupted_bulk_ids.end())
        interrupted_bulk_ids.push_back(waiting.bulk_id);
    for (auto interrupted : interrupted_bulk_ids) abort_bulk_(interrupted, this);
    queue_.clear();
    queue_.push_front(next);
    stop_pending_ = true;
  } else {
    // Coalesce only waiting targets for the SAME rail. The in-flight snapshot
    // never changes and a target for the other rail is preserved.
    for (auto it = queue_.begin(); it != queue_.end();) {
      if (mode != 9 && mode != 10 && bulk_id == 0 && it->bulk_id == 0 &&
          ((mode == 0 && it->mode == 0 && it->rail == rail) || (mode != 0 && it->mode == mode)))
        it = queue_.erase(it);
      else
        ++it;
    }
    if (queue_.size() >= 8) {
      ESP_LOGW(TAG, "%s queue full; command %lu rejected", label_.c_str(), (unsigned long) next.id);
      if (status_) status_->publish_state("queue_full: command rejected");
      return;
    }
    queue_.push_back(next);
  }
  ESP_LOGI(TAG, "%s queued cmd=%lu bulk=%lu mode=%d rail=%d target=%d depth=%u", label_.c_str(),
           (unsigned long) next.id, (unsigned long) bulk_id, mode, rail, target, unsigned(queue_.size()));
}

void GraberDispatcher::enqueue_bulk(int mode, std::initializer_list<GraberDispatcher *> motors) {
  if (mode != 3 && mode != 4 && mode != 5 && mode != 9 && mode != 10) {
    ESP_LOGW(TAG, "bulk rejected unsupported mode=%d", mode);
    return;
  }
  BulkOperation operation;
  operation.id = ++next_bulk_id_;
  operation.queued = millis();
  for (auto *motor : motors) {
    if (motor == nullptr || std::find(operation.motors.begin(), operation.motors.end(), motor) != operation.motors.end())
      continue;
    operation.motors.push_back(motor);
  }
  if (operation.motors.empty()) return;
  for (auto *motor : operation.motors) {
    if ((mode == 9 || mode == 10) && !motor->dual_rail_) {
      ESP_LOGW(TAG, "bulk rejected because %s is not a TDBU motor", motor->label_.c_str());
      if (motor->status_) motor->status_->publish_state("bulk_rejected: stack shift requires TDBU motor");
      return;
    }
    if (motor->queue_.size() >= 8) {
      ESP_LOGW(TAG, "bulk=%lu rejected because %s queue is full", (unsigned long) operation.id,
               motor->label_.c_str());
      if (motor->status_) motor->status_->publish_state("bulk_rejected: participant queue full");
      return;
    }
  }
  const auto bulk_id = operation.id;
  bulk_operations_.push_back(std::move(operation));
  for (auto *motor : find_bulk_(bulk_id)->motors) motor->enqueue_(mode, 0, 0, 0, bulk_id);
  ESP_LOGI(TAG, "bulk=%lu queued mode=%d participants=%u", (unsigned long) bulk_id, mode,
           unsigned(find_bulk_(bulk_id)->motors.size()));
}

GraberDispatcher::BulkOperation *GraberDispatcher::find_bulk_(uint32_t bulk_id) {
  for (auto &operation : bulk_operations_)
    if (operation.id == bulk_id) return &operation;
  return nullptr;
}

void GraberDispatcher::try_release_bulk_(uint32_t bulk_id) {
  auto *operation = find_bulk_(bulk_id);
  if (operation == nullptr || operation->released || operation->failed) return;
  for (auto *motor : operation->motors) {
    if (!motor->active_valid_ || motor->active_.bulk_id != bulk_id || !motor->ready_ ||
        motor->phase_ != Phase::READY)
      return;
  }
  operation->released = true;
  ESP_LOGI(TAG, "bulk=%lu all participants authenticated; releasing %u commands after %lums",
           (unsigned long) bulk_id, unsigned(operation->motors.size()),
           (unsigned long) (millis() - operation->queued));
  for (auto *motor : operation->motors) motor->report_("bulk_released");
}

void GraberDispatcher::abort_bulk_(uint32_t bulk_id, GraberDispatcher *failed_motor) {
  auto *operation = find_bulk_(bulk_id);
  if (operation == nullptr || operation->released || operation->failed) return;
  operation->failed = true;
  ESP_LOGW(TAG, "bulk=%lu aborted before dispatch by %s", (unsigned long) bulk_id,
           failed_motor == nullptr ? "unknown" : failed_motor->label_.c_str());
  for (auto *motor : operation->motors) {
    motor->queue_.erase(std::remove_if(motor->queue_.begin(), motor->queue_.end(),
                                      [bulk_id](const Command &command) { return command.bulk_id == bulk_id; }),
                        motor->queue_.end());
    if (motor->active_valid_ && motor->active_.bulk_id == bulk_id && !motor->delivered_) {
      motor->report_(motor == failed_motor ? "bulk_stage_failed" : "bulk_aborted_before_dispatch");
      motor->active_valid_ = false;
      motor->delivered_ = false;
      if (motor->ready_) motor->phase_ = Phase::READY;
    }
    if (std::find(operation->finished.begin(), operation->finished.end(), motor) == operation->finished.end())
      operation->finished.push_back(motor);
  }
  prune_bulk_operations_();
}

void GraberDispatcher::finish_bulk_participant_(uint32_t bulk_id, GraberDispatcher *motor, bool failed) {
  auto *operation = find_bulk_(bulk_id);
  if (operation == nullptr) return;
  if (failed && !operation->released) {
    abort_bulk_(bulk_id, motor);
    return;
  }
  operation->failed = operation->failed || failed;
  if (std::find(operation->finished.begin(), operation->finished.end(), motor) == operation->finished.end())
    operation->finished.push_back(motor);
  prune_bulk_operations_();
}

void GraberDispatcher::prune_bulk_operations_() {
  bulk_operations_.erase(
      std::remove_if(bulk_operations_.begin(), bulk_operations_.end(), [](const BulkOperation &operation) {
        return operation.finished.size() >= operation.motors.size();
      }),
      bulk_operations_.end());
}

void GraberDispatcher::request_battery() { battery_pending_ = true; }

void GraberDispatcher::request_all_batteries() {
  for (auto *motor : instances_) motor->request_battery();
}

bool GraberDispatcher::user_work_waiting_() const {
  for (auto *motor : instances_) {
    if (!motor->queue_.empty() || (motor->active_valid_ && motor->active_.mode != 2 && !motor->delivered_))
      return true;
  }
  return false;
}

void GraberDispatcher::start_next_() {
  if (active_valid_ || queue_.empty()) return;
  active_ = queue_.front();
  queue_.pop_front();
  active_valid_ = true;
  delivered_ = false;
  command_started_at_ = command_acked_at_ = 0;
  if (active_.mode == 1) stop_pending_ = false;
  report_("selected");
  if (ready_) phase_ = Phase::READY;
}

void GraberDispatcher::connect_() {
  if (connecting_ != nullptr || !active_valid_) return;
  if (reconnect_at_ != 0 && !due_(millis(), reconnect_at_)) return;
  reconnect_at_ = 0;
  auto state = parent()->state();
  if (state != espbt::ClientState::IDLE) return;
  ++active_.attempts;
  ready_ = authenticated_ = false;
  delivered_ = false;
  intentional_close_ = false;
  cache_in_use_ = cache_valid_;
  parent()->set_connection_type(cache_in_use_ ? espbt::ConnectionType::V3_WITH_CACHE
                                              : espbt::ConnectionType::V1);
  if (cache_in_use_) {
    handles_ = cached_handles_;
    cccds_ = cached_cccds_;
  } else {
    handles_.fill(0);
    cccds_.fill(0);
  }
  battery_samples_.fill(0);
  battery_sample_count_ = 0;
  connected_at_ = 0;
  connect_started_at_ = millis();
  link_open_at_ = setup_done_at_ = 0;
  op_ = OpKind::NONE;
  connecting_ = this;
  phase_ = Phase::CONNECTING;
  phase_since_ = millis();
  report_("connecting");
  parent()->connect();
  if (parent()->state() == espbt::ClientState::IDLE) fail_("connect_submit_failed");
}

void GraberDispatcher::cached_connected_() {
  // V3_WITH_CACHE deliberately skips service discovery. The handles were
  // learned and validated by a previous full connection to this same motor.
  node_state = espbt::ClientState::CONNECTED;
  if (connecting_ == this) connecting_ = nullptr;
  setup_step_ = 0;
  phase_ = Phase::SETUP;
  phase_since_ = millis();
  if (connected_at_ == 0) connected_at_ = millis();
  next_op_at_ = millis();
  report_("cached_handles_ready");
}

void GraberDispatcher::remember_handles_() {
  cached_handles_ = handles_;
  cached_cccds_ = cccds_;
  cache_valid_ = true;
  ESP_LOGI(TAG, "%s learned and validated GATT handles for fast reconnect", label_.c_str());
}

void GraberDispatcher::invalidate_cache_(const char *reason) {
  if (!cache_valid_ && !cache_in_use_) return;
  ESP_LOGW(TAG, "%s invalidating cached GATT handles after %s", label_.c_str(), reason);
  cache_valid_ = false;
  cache_in_use_ = false;
  cached_handles_.fill(0);
  cached_cccds_.fill(0);
  parent()->set_connection_type(espbt::ConnectionType::V1);
}

void GraberDispatcher::discover_() {
  static const char *const bottom_service = "de8a5aac-a99b-c315-0c80-60b63a869124";
  static const char *const top_service = "375b4079-d234-4631-9592-65c87174410c";
  static const char *const chars[] = {
    "5b026510-4088-c297-46d8-be6cb63a8691", "3ead2b49-abbc-4602-ac80-54d4b5687bac",
    "bb27521d-2fd4-4187-af9f-5d0780238d7e", "a506fbb1-9777-4e43-aa13-67d1bab6c0a1",
    "e3fc1ebf-e615-44f2-b1dd-b6b95266a453", "ebc1ed9c-e9e1-4db3-a8f5-ff249944b442",
    "900eeaeb-0f27-4808-809c-4026a8fd7d5a"
  };
  // Hold the parent's service cache through every required CCCD acknowledgement.
  node_state = espbt::ClientState::CONNECTED;
  for (int i = 0; i < BATTERY; ++i) {
    if (!dual_rail_ && (i == TOP_TARGET || i == TOP_POSITION || i == PRESET)) continue;
    auto service = espbt::ESPBTUUID::from_raw((i == EXECUTE || i == TOP_TARGET || i == TOP_POSITION)
                                              ? top_service : bottom_service);
    auto *chr = parent()->get_characteristic(service, espbt::ESPBTUUID::from_raw(chars[i]));
    if (!chr || (i <= PRESET && !(chr->properties & ESP_GATT_CHAR_PROP_BIT_WRITE))) {
      // Some single-rail motors expose the secondary Execute characteristic.
      // Preserve their deployed two-write Stop sequence only when available.
      if (!dual_rail_ && i == EXECUTE) continue;
      fail_("required_characteristic_missing_or_not_acknowledged_writable");
      return;
    }
    handles_[i] = chr->handle;
    if (i == BOTTOM_POSITION || i == TOP_POSITION) {
      auto *desc = chr->get_descriptor(uint16_t(0x2902));
      if (!desc) { fail_("notification_descriptor_missing"); return; }
      cccds_[i == BOTTOM_POSITION ? 0 : 1] = desc->handle;
    }
  }
  auto *battery = parent()->get_characteristic(uint16_t(0x180f), uint16_t(0x2a19));
  if (battery) handles_[BATTERY] = battery->handle;
  // BlueDroid supports three established links, but only one reliable
  // connection-establishment procedure at a time on this controller.
  if (connecting_ == this) connecting_ = nullptr;
  setup_step_ = 0;
  phase_ = Phase::SETUP;
  phase_since_ = millis();
  if (connected_at_ == 0) connected_at_ = millis();
  next_op_at_ = millis();
  report_("services_ready");
}

bool GraberDispatcher::write_(Handle handle, const uint8_t *value, size_t length) {
  if (!handles_[handle] || !parent()->connected()) { fail_("write_not_ready"); return false; }
  op_ = OpKind::WRITE;
  op_handle_ = handles_[handle];
  op_since_ = millis();
  auto err = esp_ble_gattc_write_char(parent()->get_gattc_if(), parent()->get_conn_id(), op_handle_, length,
                                   const_cast<uint8_t *>(value), ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE);
  if (err != ESP_OK) { fail_("write_submit_failed"); return false; }
  return true;
}

bool GraberDispatcher::read_(Handle handle) {
  if (!handles_[handle]) return false;
  op_ = OpKind::READ;
  op_handle_ = handles_[handle];
  op_since_ = millis();
  auto err = esp_ble_gattc_read_char(parent()->get_gattc_if(), parent()->get_conn_id(), op_handle_,
                                  ESP_GATT_AUTH_REQ_NONE);
  if (err != ESP_OK) {
    if (handle == BATTERY)
      ESP_LOGW(TAG, "%s battery submit failed sample=%u conn=%u handle=0x%04x status=%d len=0 raw=-1 connected_ms=%lu poll_ms=%lu",
               label_.c_str(), unsigned(battery_sample_count_ + 1), unsigned(parent()->get_conn_id()),
               unsigned(op_handle_), int(err), (unsigned long) (millis() - connected_at_),
               (unsigned long) (millis() - battery_started_at_));
    fail_("read_submit_failed");
    return false;
  }
  return true;
}

bool GraberDispatcher::subscribe_(int rail) {
  op_ = OpKind::SUBSCRIBE;
  op_handle_ = cccds_[rail];
  op_since_ = millis();
  // BLEClientBase handles REG_FOR_NOTIFY by writing the CCCD. This node
  // remains CONNECTED until descriptor ACKs arrive so no cache is freed early.
  auto err = esp_ble_gattc_register_for_notify(parent()->get_gattc_if(), parent()->get_remote_bda(),
                                              handles_[rail == 0 ? BOTTOM_POSITION : TOP_POSITION]);
  if (err != ESP_OK) { fail_("notify_register_submit_failed"); return false; }
  return true;
}

void GraberDispatcher::run_setup_() {
  const uint8_t subscription_count = dual_rail_ ? 2 : 1;
  if (setup_step_ < subscription_count) { subscribe_(setup_step_); return; }
  if (setup_step_ == subscription_count) {
    char token[8];
    snprintf(token, sizeof(token), "%07d", token_);
    write_(AUTH, reinterpret_cast<uint8_t *>(token), 7);
    return;
  }
  authenticated_ = ready_ = true;
  setup_done_at_ = millis();
  if (!cache_in_use_) remember_handles_();
  node_state = espbt::ClientState::ESTABLISHED;
  phase_ = Phase::READY;
  report_("subscriptions_ready_auth_write_acknowledged");
}

void GraberDispatcher::prepare_command_() {
  if (!active_valid_) return;
  if (active_.attempts == 0) active_.attempts = 1;  // existing connected session
  steps_.clear();
  step_ = 0;
  delivered_ = observed_change_ = feedback_logged_ = false;
  command_samples_ = samples_;
  last_changed_at_ = millis();
  battery_requested_ = false;
  battery_samples_.fill(0);
  battery_sample_count_ = 0;
  battery_started_at_ = millis();
  command_started_at_ = millis();
  command_acked_at_ = 0;
  switch (active_.mode) {
    case 0:
      steps_.push_back({active_.rail == 0 ? BOTTOM_TARGET : TOP_TARGET, active_.target, 100});
      steps_.push_back({active_.rail == 0 ? AUTH : EXECUTE, 0x14, 0});
      break;
    case 1:
      steps_ = {{AUTH, 0x10, uint16_t(handles_[EXECUTE] ? 100 : 0)}};
      if (handles_[EXECUTE]) steps_.push_back({EXECUTE, 0x10, 0});
      break;
    case 2: break;
    case 3: steps_ = {{AUTH, 0x16, 0}}; break;
    case 4:
      if (dual_rail_) steps_ = {{AUTH, 0x12, 0}};
      else steps_ = {{BOTTOM_TARGET, 100, 100}, {AUTH, 0x14, 0}};
      break;
    // Isolated Android close capture: Execute 12, THEN Auth 13 (45–55ms).
    case 5:
      if (dual_rail_) steps_ = {{EXECUTE, 0x12, 50}, {AUTH, 0x13, 0}};
      else steps_ = {{BOTTOM_TARGET, 0, 100}, {AUTH, 0x14, 0}};
      break;
    case 6: steps_ = {{EXECUTE, 0x16, 0}}; break;
    case 7: steps_ = {{PRESET, 0x05, 50}, {AUTH, 0x15, 0}}; break;
    case 9:
    case 10:
      // App-captured shared stack steps: both targets are fixed at 5, and
      // execute 0x18/0x19 selects direction. Keep the writes close together.
      steps_ = {{BOTTOM_TARGET, 0x05, 70}, {TOP_TARGET, 0x05, 70},
                {EXECUTE, uint8_t(active_.mode == 9 ? 0x18 : 0x19), 0}};
      break;
  }
  phase_ = Phase::COMMAND;
  next_op_at_ = millis();
  report_("sending");
}

void GraberDispatcher::run_command_() {
  if (active_.mode == 2) {
    if (!handles_[BATTERY]) { battery_pending_ = false; finish_("battery_characteristic_missing"); return; }
    static constexpr uint32_t SAMPLE_OFFSETS[] = {0, 1000, 3000};
    if (!battery_requested_ && battery_sample_count_ < battery_samples_.size() &&
        millis() - battery_started_at_ >= SAMPLE_OFFSETS[battery_sample_count_]) {
      battery_requested_ = true;
      ESP_LOGI(TAG, "%s battery read sample=%u conn=%u handle=0x%04x connected_ms=%lu poll_ms=%lu",
               label_.c_str(), unsigned(battery_sample_count_ + 1), unsigned(parent()->get_conn_id()),
               unsigned(handles_[BATTERY]), (unsigned long) (millis() - connected_at_),
               (unsigned long) (millis() - battery_started_at_));
      read_(BATTERY);
    }
    return;
  }
  if (step_ >= steps_.size()) { mark_delivered_(); return; }
  auto &step = steps_[step_];
  write_(step.handle, &step.value, 1);
}

void GraberDispatcher::operation_ok_() {
  op_ = OpKind::NONE;
  if (phase_ == Phase::SETUP) {
    ++setup_step_;
    next_op_at_ = millis();
  } else if (phase_ == Phase::COMMAND && active_.mode != 2) {
    // Minimum app-observed inter-write interval starts when sent; do not add
    // another full delay after a slow ATT acknowledgement.
    next_op_at_ = op_since_ + steps_[step_].settle_ms;
    ++step_;
    report_("write_acknowledged");
    if (step_ == steps_.size()) mark_delivered_();
  }
}

void GraberDispatcher::mark_delivered_() {
  delivered_ = true;
  delivered_at_ = millis();
  command_acked_at_ = delivered_at_;
  // Stack-shift completion must compare feedback with the snapshot taken
  // before any target/execute writes; early notifications can arrive before
  // the final ATT acknowledgement.
  if (active_.mode != 9 && active_.mode != 10) command_samples_ = samples_;
  last_read_at_ = 0;
  read_rail_ = 0;
  phase_ = Phase::OBSERVE;
  report_("delivered_waiting_for_fresh_position");
}

void GraberDispatcher::sample_(int rail, const uint8_t *value, uint16_t length) {
  if (!dual_rail_ && rail != 0) return;
  if (length != 1 || value[0] > 100) return;
  const float position = value[0];
  ++samples_[rail];
  sampled_at_[rail] = millis();
  last_sample_at_ = millis();
  if (std::isfinite(last_values_[rail]) && last_values_[rail] != position) {
    last_changed_at_ = millis();
    if (active_valid_) observed_change_ = true;
  }
  last_values_[rail] = position;
  if (positions_[rail]) positions_[rail]->publish_state(position);
  if (notify_sensors_[rail]) notify_sensors_[rail]->publish_state(position);
  if (active_valid_ && delivered_ && !feedback_logged_) {
    feedback_logged_ = true;
    report_("fresh_position_received");
  }
}

bool GraberDispatcher::desired_reached_() const {
  const bool bottom_fresh = samples_[0] != command_samples_[0];
  const bool top_fresh = samples_[1] != command_samples_[1];
  auto matches = [this](int rail, int target) { return std::isfinite(last_values_[rail]) &&
                                                    fabsf(last_values_[rail] - target) <= 1.0f; };
  switch (active_.mode) {
    case 0: return (active_.rail == 0 ? bottom_fresh : top_fresh) && matches(active_.rail, active_.target);
    case 4: return bottom_fresh && matches(0, 100) && (!dual_rail_ || (top_fresh && matches(1, 100)));
    case 5: return bottom_fresh && matches(0, 0) && (!dual_rail_ || (top_fresh && matches(1, 100)));
    case 6: return bottom_fresh && top_fresh && matches(0, 0) && matches(1, 50);
    case 7: return bottom_fresh && top_fresh && matches(0, 50) && matches(1, 100);
    case 9:
    case 10: return bottom_fresh && top_fresh;
    default: return false;
  }
}

void GraberDispatcher::observe_() {
  const auto now = millis();
  // A target match must be fresh, and stable for a short settling interval.
  if (desired_reached_() && now - last_changed_at_ >= 600 && now - delivered_at_ >= 600) {
    finish_("target_verified");
    return;
  }
  if ((active_.mode == 1 || active_.mode == 3) && samples_[0] != command_samples_[0] &&
      (!dual_rail_ || samples_[1] != command_samples_[1]) && now - delivered_at_ >= 2000 &&
      now - last_changed_at_ >= 2000 && (active_.mode == 1 || observed_change_)) {
    finish_(active_.mode == 1 ? "stop_acknowledged_position_stable" : "favorite_acknowledged_position_stable");
    return;
  }
  if (now - delivered_at_ > OBSERVE_LIMIT) {
    finish_("delivered_completion_unconfirmed_timeout");
    close_();
    return;
  }
  // Notifications are primary. A quiet stream gets occasional explicit reads,
  // which also verify a no-op target without inventing movement from stale ads.
  if (now - last_read_at_ >= 1200) {
    // One active notification rail must not starve the other rail's read.
    const int rail = !dual_rail_ || now - sampled_at_[0] >= now - sampled_at_[1] ? 0 : 1;
    if (now - sampled_at_[rail] >= 1200)
      read_(rail == 0 ? BOTTOM_POSITION : TOP_POSITION);
    last_read_at_ = now;
  }
}

void GraberDispatcher::finish_(const char *result) {
  const uint32_t bulk_id = active_.bulk_id;
  report_(result);
  active_valid_ = false;
  delivered_ = false;
  phase_ = ready_ ? Phase::READY : Phase::IDLE;
  phase_since_ = millis();
  if (bulk_id) finish_bulk_participant_(bulk_id, this, false);
}

void GraberDispatcher::close_() {
  ready_ = authenticated_ = false;
  intentional_close_ = true;
  op_ = OpKind::NONE;
  phase_ = Phase::CLOSING;
  phase_since_ = millis();
  if (parent()->state() == espbt::ClientState::CONNECTING) {
    esp_ble_gattc_cancel_open_params_t cancel{};
    cancel.gattc_if = parent()->get_gattc_if();
    memcpy(cancel.remote_bda, parent()->get_remote_bda(), sizeof(cancel.remote_bda));
    if (esp_ble_gattc_cancel_open(&cancel) != ESP_OK)
      report_("connect_cancel_submit_failed_waiting_for_cleanup");
  } else {
    parent()->disconnect();
  }
}

void GraberDispatcher::fail_(const char *reason) {
  report_(reason);
  // A handle/CCCD problem on the fast path may mean the motor's GATT database
  // changed. The normal retry performs full discovery and relearns it.
  if (cache_in_use_ && phase_ != Phase::CONNECTING) invalidate_cache_(reason);
  // Never retry a transaction whose last write has been acknowledged: feedback
  // failure does not establish that movement failed. Preserve that distinction.
  if (active_valid_ && (delivered_ || active_.attempts >= 3 || active_.mode == 2)) {
    const uint32_t bulk_id = active_.bulk_id;
    report_(delivered_ ? "delivered_feedback_unconfirmed" : "failed_attempt_limit");
    if (active_.mode == 2) {
      battery_pending_ = false;
      battery_at_ = millis();
      battery_has_read_ = true;
    }
    active_valid_ = false;
    if (bulk_id) finish_bulk_participant_(bulk_id, this, true);
  } else if (active_valid_) {
    report_("retry_after_disconnect");
  }
  if (active_valid_ && active_.attempts >= 3 &&
      (strcmp(reason, "connect_timeout") == 0 || strcmp(reason, "connection_open_failed") == 0 ||
       strcmp(reason, "service_discovery_failed") == 0 || strcmp(reason, "gatt_operation_timeout") == 0)) {
    reboot_after_cleanup_ = true;
  }
  reconnect_at_ = millis() + 350;
  close_();
}

void GraberDispatcher::disconnected_() {
  if (connecting_ == this) connecting_ = nullptr;
  const bool unexpected = !intentional_close_;
  ready_ = authenticated_ = false;
  op_ = OpKind::NONE;
  phase_ = Phase::IDLE;
  phase_since_ = millis();
  if (unexpected && active_valid_) {
    if (delivered_ || active_.attempts >= 3) {
      const bool failed_before_delivery = !delivered_;
      const uint32_t bulk_id = active_.bulk_id;
      finish_(delivered_ ? "delivered_disconnected_completion_unconfirmed" : "failed_attempt_limit");
      if (failed_before_delivery && bulk_id) finish_bulk_participant_(bulk_id, this, true);
    } else {
      report_("link_lost_retry_pending");
      reconnect_at_ = millis() + 350;
    }
  }
  intentional_close_ = false;
}

void GraberDispatcher::loop() {
  const auto now = millis();
  // BLEClient also calls node.loop(); avoid processing twice in the same tick.
  if (now == last_loop_) return;
  last_loop_ = now;
  if ((!battery_has_read_ && now > 30000) || (battery_has_read_ && now - battery_at_ >= BATTERY_INTERVAL))
    battery_pending_ = true;
  if (op_ != OpKind::NONE) {
    if (now - op_since_ >= OP_TIMEOUT) {
      if (op_ == OpKind::READ && op_handle_ == handles_[BATTERY])
        ESP_LOGW(TAG, "%s battery timeout sample=%u conn=%u handle=0x%04x status=no_response len=0 raw=-1 connected_ms=%lu poll_ms=%lu",
                 label_.c_str(), unsigned(battery_sample_count_ + 1), unsigned(parent()->get_conn_id()),
                 unsigned(op_handle_), (unsigned long) (now - connected_at_),
                 (unsigned long) (now - battery_started_at_));
      fail_("gatt_operation_timeout");
    }
    return;
  }
  // A battery transaction may yield only after its ATT operation drains. This
  // keeps a late read response from being attributed to a user command.
  if (active_valid_ && active_.mode == 2 && !queue_.empty()) {
    report_("battery_yielded_to_user_command");
    active_valid_ = false;
    delivered_ = false;
    battery_pending_ = true;
    battery_requested_ = false;
    battery_sample_count_ = 0;
    battery_samples_.fill(0);
    if (ready_) phase_ = Phase::READY;
  }
  // Stop may preempt only after draining the outstanding ATT operation, so a
  // late ACK cannot be mistaken for acknowledgement of the Stop write.
  if (stop_pending_) {
    if (active_valid_) report_("superseded_by_stop");
    active_valid_ = false;
    delivered_ = false;
    start_next_();
  }
  if (active_valid_ && delivered_ && !queue_.empty()) {
    finish_("delivered_observation_superseded_by_new_command");
  }
  start_next_();
  if (phase_ == Phase::CLOSING) {
    if (parent()->state() == espbt::ClientState::IDLE) {
      disconnected_();
      if (reboot_after_cleanup_) {
        reboot_after_cleanup_ = false;
        ESP_LOGW(TAG, "%s BLE watchdog restarting controller after repeated link failures", label_.c_str());
        App.safe_reboot();
      }
    }
    else if (now - phase_since_ >= 30000) {
      // Never reuse a connection slot that the BLE stack has not released.
      // A rare wedged stack needs a controller restart, not stale command replay.
      report_("failed_ble_cleanup_restarting_controller");
      App.safe_reboot();
    }
    return;
  }
  if (phase_ == Phase::CONNECTING) {
    if (now - phase_since_ >= CONNECT_TIMEOUT) fail_("connect_timeout");
    return;
  }
  if (phase_ == Phase::SETUP) {
    if (due_(now, next_op_at_)) run_setup_();
    return;
  }
  if (!active_valid_ && battery_pending_ && !user_work_waiting_()) {
    battery_pending_ = false;
    battery_has_read_ = true;
    battery_at_ = now;
    active_ = Command{++next_id_, now, 0, 2, 0, 0, 0, 0};
    active_valid_ = true;
    delivered_ = false;
    report_("battery_selected_low_priority");
  }
  if (!ready_) { connect_(); return; }
  if (active_valid_) {
    if (phase_ == Phase::COMMAND && due_(now, next_op_at_)) run_command_();
    else if (phase_ == Phase::OBSERVE) observe_();
    else if (phase_ == Phase::READY) {
      if (active_.bulk_id) {
        try_release_bulk_(active_.bulk_id);
        auto *operation = find_bulk_(active_.bulk_id);
        if (operation == nullptr || operation->failed || !operation->released) return;
      }
      prepare_command_();
    }
  } else if (now - phase_since_ >= IDLE_CONNECTION_HOLD) {
    close_();
  }
}

void GraberDispatcher::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t,
                                           esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_SEARCH_CMPL_EVT:
      if (phase_ != Phase::CONNECTING) break;
      if (param->search_cmpl.status != ESP_GATT_OK) { fail_("service_discovery_failed"); break; }
      discover_();
      break;
    case ESP_GATTC_OPEN_EVT:
      if (phase_ == Phase::CONNECTING && param->open.status == ESP_GATT_OK) {
        connected_at_ = millis();
        link_open_at_ = connected_at_;
        if (cache_in_use_) cached_connected_();
      }
      if (phase_ == Phase::CONNECTING && param->open.status != ESP_GATT_OK) fail_("connection_open_failed");
      else if (phase_ == Phase::CLOSING) {
        if (param->open.status == ESP_GATT_OK) parent()->disconnect();
        else if (parent()->state() == espbt::ClientState::IDLE) disconnected_();
      }
      break;
    case ESP_GATTC_CANCEL_OPEN_EVT:
      if (phase_ != Phase::CLOSING ||
          memcmp(param->cancel_open.remote_bda, parent()->get_remote_bda(), 6) != 0) break;
      if (param->cancel_open.status == ESP_GATT_OK && parent()->state() == espbt::ClientState::CONNECTING) {
        parent()->release_services();
        parent()->set_state(espbt::ClientState::IDLE);
        disconnected_();
      }
      break;
    case ESP_GATTC_DISCONNECT_EVT:
    case ESP_GATTC_CLOSE_EVT:
      // Base filters connection IDs. CLOSE after DISCONNECT is harmless.
      if (phase_ != Phase::IDLE && phase_ != Phase::CLOSING) {
        disconnected_();
        // Base may be DISCONNECTING until CLOSE; do not start new work yet.
        if (parent()->state() != espbt::ClientState::IDLE) {
          intentional_close_ = true;
          phase_ = Phase::CLOSING;
          phase_since_ = millis();
        }
      } else if (phase_ == Phase::CLOSING && parent()->state() == espbt::ClientState::IDLE) {
        disconnected_();
      }
      break;
    case ESP_GATTC_REG_FOR_NOTIFY_EVT:
      if (op_ != OpKind::SUBSCRIBE) break;
      if (param->reg_for_notify.status != ESP_GATT_OK) {
        fail_("notify_register_failed");
      } else if (cache_in_use_) {
        // V1 resolves and writes the CCCD in BLEClientBase. Cache mode has no
        // service tree to walk, so write the learned CCCD handle directly.
        uint16_t notify_enable = 1;
        op_since_ = millis();
        auto err = esp_ble_gattc_write_char_descr(
            parent()->get_gattc_if(), parent()->get_conn_id(), op_handle_, sizeof(notify_enable),
            reinterpret_cast<uint8_t *>(&notify_enable), ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE);
        if (err != ESP_OK) fail_("notify_descriptor_submit_failed");
      }
      break;
    case ESP_GATTC_WRITE_DESCR_EVT:
      if (param->write.conn_id != parent()->get_conn_id()) break;
      if (op_ != OpKind::SUBSCRIBE || param->write.handle != op_handle_) break;
      if (param->write.status != ESP_GATT_OK) { fail_("notify_descriptor_write_failed"); break; }
      operation_ok_();
      break;
    case ESP_GATTC_WRITE_CHAR_EVT:
      if (param->write.conn_id != parent()->get_conn_id()) break;
      if (op_ != OpKind::WRITE || param->write.handle != op_handle_) break;
      if (param->write.status != ESP_GATT_OK) { fail_("write_ack_failed"); break; }
      operation_ok_();
      break;
    case ESP_GATTC_READ_CHAR_EVT:
      if (param->read.conn_id != parent()->get_conn_id()) break;
      if (op_ != OpKind::READ || param->read.handle != op_handle_) break;
      if (op_handle_ == handles_[BATTERY]) {
        const bool has_byte = param->read.status == ESP_GATT_OK && param->read.value_len > 0 && param->read.value;
        ESP_LOGI(TAG, "%s battery response sample=%u conn=%u handle=0x%04x status=%u len=%u raw=%d connected_ms=%lu poll_ms=%lu",
                 label_.c_str(), unsigned(battery_sample_count_ + 1), unsigned(param->read.conn_id),
                 unsigned(param->read.handle), unsigned(param->read.status), unsigned(param->read.value_len),
                 has_byte ? int(param->read.value[0]) : -1, (unsigned long) (millis() - connected_at_),
                 (unsigned long) (millis() - battery_started_at_));
      }
      if (param->read.status != ESP_GATT_OK) { fail_("read_failed"); break; }
      if (op_handle_ == handles_[BOTTOM_POSITION]) sample_(0, param->read.value, param->read.value_len);
      else if (op_handle_ == handles_[TOP_POSITION]) sample_(1, param->read.value, param->read.value_len);
      else if (op_handle_ == handles_[BATTERY]) {
        if (param->read.value_len == 1 && param->read.value && param->read.value[0] <= 100 &&
            battery_sample_count_ < battery_samples_.size()) {
          battery_samples_[battery_sample_count_++] = param->read.value[0];
          battery_requested_ = false;
          if (battery_sample_count_ == battery_samples_.size()) {
            battery_has_read_ = true;
            battery_at_ = millis();
            battery_pending_ = false;
            if (battery_samples_[1] == battery_samples_[2]) {
              if (battery_) battery_->publish_state(battery_samples_[2]);
              finish_("battery_read_confirmed");
            } else {
              finish_("battery_unstable_preserving_previous");
            }
          }
        } else {
          fail_("invalid_battery_read");
          break;
        }
      }
      op_ = OpKind::NONE;
      break;
    case ESP_GATTC_NOTIFY_EVT:
      if (param->notify.conn_id != parent()->get_conn_id() || phase_ == Phase::CLOSING || phase_ == Phase::IDLE) break;
      if (param->notify.handle == handles_[BOTTOM_POSITION]) sample_(0, param->notify.value, param->notify.value_len);
      else if (param->notify.handle == handles_[TOP_POSITION]) sample_(1, param->notify.value, param->notify.value_len);
      break;
    default: break;
  }
}

}  // namespace esphome::graber_dispatcher
