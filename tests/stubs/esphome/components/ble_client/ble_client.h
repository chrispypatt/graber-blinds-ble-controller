#pragma once
#include "esphome/core/component.h"
#include <map>
#include <cstring>
using esp_err_t = int;
using esp_gatt_if_t = int;
using esp_gatt_status_t = int;
using esp_bd_addr_t = uint8_t[6];
constexpr int ESP_OK = 0, ESP_GATT_OK = 0;
constexpr int ESP_GATT_AUTH_REQ_NONE = 0;
constexpr int ESP_GATT_WRITE_TYPE_RSP = 1;
constexpr int ESP_GATT_CHAR_PROP_BIT_WRITE = 8;
constexpr int ESP_GATT_CHAR_PROP_BIT_NOTIFY = 16;
constexpr int ESP_GATT_UUID_CHAR_CLIENT_CONFIG = 0x2902;
constexpr int ESP_GATT_IF_NONE = -1;
enum esp_gattc_cb_event_t {
 ESP_GATTC_OPEN_EVT, ESP_GATTC_CLOSE_EVT, ESP_GATTC_DISCONNECT_EVT,
 ESP_GATTC_SEARCH_CMPL_EVT, ESP_GATTC_WRITE_CHAR_EVT, ESP_GATTC_WRITE_DESCR_EVT,
 ESP_GATTC_READ_CHAR_EVT, ESP_GATTC_REG_FOR_NOTIFY_EVT, ESP_GATTC_NOTIFY_EVT,
 ESP_GATTC_CANCEL_OPEN_EVT, ESP_GATTC_CONNECT_EVT
};
struct esp_ble_gattc_cb_param_t {
 struct { int status=0; uint16_t conn_id=0, handle=0; } write, search_cmpl, open, close, reg_for_notify;
 struct { int status=0; uint16_t conn_id=0, handle=0, value_len=0; uint8_t *value=nullptr; } read, notify;
 struct { uint16_t conn_id=0; uint8_t reason=0; uint8_t remote_bda[6]{}; } disconnect;
 struct { int status=0; uint8_t remote_bda[6]{}; } cancel_open;
};
struct esp_ble_gattc_cancel_open_params_t { int gattc_if=0; uint8_t remote_bda[6]{}; };
namespace test_ble {
struct Request { int interface; uint16_t connection, handle; std::vector<uint8_t> value; bool read; };
inline std::vector<Request> requests;
inline int next_error = ESP_OK;
inline int cancellations=0;
inline int submit(int interface, uint16_t conn, uint16_t handle, uint16_t len, uint8_t *bytes, bool read) {
  if (next_error != ESP_OK) { const auto result = next_error; next_error = ESP_OK; return result; }
  requests.push_back({interface, conn, handle, bytes ? std::vector<uint8_t>(bytes, bytes+len) : std::vector<uint8_t>(),read});
  return ESP_OK;
}
}
inline int esp_ble_gattc_write_char(int interface, uint16_t conn, uint16_t handle, uint16_t len, uint8_t *bytes, int, int) {
 return test_ble::submit(interface, conn, handle, len, bytes, false);
}
inline int esp_ble_gattc_write_char_descr(int interface, uint16_t conn, uint16_t handle, uint16_t len,
                                         uint8_t *bytes, int, int) {
 return test_ble::submit(interface, conn, handle, len, bytes, false);
}
inline int esp_ble_gattc_read_char(int interface, uint16_t conn, uint16_t handle, int) {
 return test_ble::submit(interface, conn, handle, 0, nullptr, true);
}
inline const char *esp_err_to_name(int) { return "stub_error"; }
inline int esp_ble_gattc_register_for_notify(int, uint8_t *, uint16_t) { return ESP_OK; }
inline int esp_ble_gattc_cancel_open(const esp_ble_gattc_cancel_open_params_t *) { ++test_ble::cancellations; return ESP_OK; }
namespace esphome::esp32_ble_tracker {
enum class ClientState { INIT, IDLE, DISCOVERED, CONNECTING, CONNECTED, ESTABLISHED, DISCONNECTING };
enum class ConnectionType { V1, V3_WITH_CACHE, V3_WITHOUT_CACHE };
class ESPBTUUID {
 public:
  std::string value;
  static ESPBTUUID from_raw(const std::string &v) { return {v}; }
  static ESPBTUUID from_raw(const char *v) { return {v}; }
  static ESPBTUUID from_uint16(uint16_t v) { return {std::to_string(v)}; }
};
}
namespace esphome::esp32_ble_client {
struct BLEDescriptor { uint16_t handle=0; };
struct BLECharacteristic {
 uint16_t handle=0;
 int properties=ESP_GATT_CHAR_PROP_BIT_WRITE | ESP_GATT_CHAR_PROP_BIT_NOTIFY;
 BLEDescriptor descriptor;
 BLEDescriptor *get_descriptor(uint16_t) { return descriptor.handle ? &descriptor : nullptr; }
 BLEDescriptor *get_descriptor(esp32_ble_tracker::ESPBTUUID) { return descriptor.handle ? &descriptor : nullptr; }
};
}
namespace esphome::ble_client {
namespace espbt = esp32_ble_tracker;
class BLEClient;
class BLEClientNode {
 public:
  espbt::ClientState node_state = espbt::ClientState::IDLE;
  virtual ~BLEClientNode() = default;
  virtual void gattc_event_handler(esp_gattc_cb_event_t,int,esp_ble_gattc_cb_param_t*) {}
  BLEClient *parent() { return parent_; }
  void set_ble_client_parent(BLEClient *p) { parent_=p; }
 protected:
  BLEClient *parent_ = nullptr;
};
class BLEClient {
 public:
  int interface=1;
  uint16_t connection=1;
  int connects=0, disconnects=0;
  espbt::ClientState state_value = espbt::ClientState::IDLE;
  espbt::ConnectionType connection_type = espbt::ConnectionType::V1;
  std::map<std::string, esp32_ble_client::BLECharacteristic> chars;
  std::vector<uint16_t> subscriptions;
  std::vector<std::function<void()>> deferred;
  BLEClientNode *node=nullptr;
  uint8_t address[6]{};
  void register_ble_node(BLEClientNode *n) { node=n; n->set_ble_client_parent(this); }
  espbt::ClientState state() const { return state_value; }
  void set_state(espbt::ClientState s) { state_value=s; if(node) node->node_state=s; }
  void set_connection_type(espbt::ConnectionType type) { connection_type=type; }
  bool connected() const { return state_value==espbt::ClientState::ESTABLISHED; }
  void connect() { ++connects; set_state(espbt::ClientState::CONNECTING); }
  void disconnect() { ++disconnects; set_state(espbt::ClientState::DISCONNECTING); }
  int get_gattc_if() const { return interface; }
  uint16_t get_conn_id() const { return connection; }
  const char *address_str() const { return "STUB"; }
  uint8_t *get_remote_bda() { return address; }
  esp32_ble_client::BLECharacteristic *get_characteristic(espbt::ESPBTUUID, espbt::ESPBTUUID c) {
    const auto it=chars.find(c.value); return it==chars.end() ? nullptr : &it->second;
  }
  esp32_ble_client::BLECharacteristic *get_characteristic(uint16_t service, uint16_t c) {
    return get_characteristic(espbt::ESPBTUUID::from_uint16(service),espbt::ESPBTUUID::from_uint16(c));
  }
  int register_for_notify(uint16_t handle) { subscriptions.push_back(handle); return ESP_OK; }
  void run_later(std::function<void()> &&f) { deferred.push_back(std::move(f)); }
  void release_services() {}
};
}
