#include "graber_dispatcher.h"
#include "esphome/core/application.h"
#include <iostream>
#include <stdexcept>
#include <initializer_list>
using namespace esphome;
using namespace esphome::graber_dispatcher;

#define CHECK(condition) do { if (!(condition)) throw std::runtime_error(std::string(__func__) + ":" + std::to_string(__LINE__) + ": " #condition); } while (0)

class TestedDispatcher : public GraberDispatcher {
 public:
  using GraberDispatcher::Command;
  using GraberDispatcher::Phase;
  using GraberDispatcher::OpKind;
  using GraberDispatcher::Handle;
  using GraberDispatcher::queue_;
  using GraberDispatcher::active_;
  using GraberDispatcher::active_valid_;
  using GraberDispatcher::phase_;
  using GraberDispatcher::op_;
  using GraberDispatcher::op_handle_;
  using GraberDispatcher::step_;
  using GraberDispatcher::steps_;
  using GraberDispatcher::ready_;
  using GraberDispatcher::authenticated_;
  using GraberDispatcher::stop_pending_;
  using GraberDispatcher::connecting_;
  using GraberDispatcher::bulk_operations_;
  using GraberDispatcher::next_bulk_id_;
  using GraberDispatcher::handles_;
  using GraberDispatcher::samples_;
  using GraberDispatcher::cccds_;
  using GraberDispatcher::cache_valid_;
  using GraberDispatcher::cache_in_use_;
  using GraberDispatcher::cached_handles_;
  using GraberDispatcher::cached_cccds_;
  using GraberDispatcher::phase_since_;
  using GraberDispatcher::battery_pending_;
  using GraberDispatcher::delivered_;
  using GraberDispatcher::next_op_at_;
  using GraberDispatcher::reconnect_at_;
  using GraberDispatcher::last_values_;
  using GraberDispatcher::last_changed_at_;
  using GraberDispatcher::command_samples_;
  using GraberDispatcher::delivered_at_;
  using GraberDispatcher::desired_reached_;
  using GraberDispatcher::battery_has_read_;
  using GraberDispatcher::battery_at_;
  using GraberDispatcher::battery_started_at_;
  using GraberDispatcher::battery_sample_count_;
  using GraberDispatcher::start_next_;
  using GraberDispatcher::prepare_command_;
  using GraberDispatcher::fail_;
  static void reset_globals() {
    instances_.clear(); connecting_=nullptr; bulk_operations_.clear(); next_id_=0; next_bulk_id_=0;
    test_now=1000; test_ble::requests.clear(); test_ble::next_error=0;
    test_ble::cancellations=0; App.reboots=0;
    test_log::messages.clear();
  }
  void force_ready() {
    parent()->set_state(esp32_ble_tracker::ClientState::ESTABLISHED);
    node_state=esp32_ble_tracker::ClientState::ESTABLISHED;
    phase_=Phase::READY; phase_since_=millis(); ready_=true; authenticated_=true;
    const uint16_t values[] = {0x1d,0x24,0x85,0x8c,0x26,0x21,0x89,0x58};
    for (size_t i=0;i<handles_.size();++i) handles_[i]=values[i];
    cccds_={0x22,0x8a};
  }
};

struct Fixture {
  ble_client::BLEClient client;
  TestedDispatcher d;
  sensor::Sensor bottom,top,battery;
  text_sensor::TextSensor status;
  explicit Fixture(int number=1, bool dual_rail=true) {
    client.interface=number; client.connection=number;
    client.address[5]=number;
    client.register_ble_node(&d);
    d.set_token(1234567); d.set_label("test"+std::to_string(number));
    d.set_dual_rail(dual_rail);
    d.set_bottom_sensor(&bottom); d.set_top_sensor(&top);
    d.set_battery_sensor(&battery); d.set_status_sensor(&status);
    d.setup();
    const char *ids[] = {
      "5b026510-4088-c297-46d8-be6cb63a8691","3ead2b49-abbc-4602-ac80-54d4b5687bac",
      "bb27521d-2fd4-4187-af9f-5d0780238d7e","a506fbb1-9777-4e43-aa13-67d1bab6c0a1",
      "e3fc1ebf-e615-44f2-b1dd-b6b95266a453","ebc1ed9c-e9e1-4db3-a8f5-ff249944b442",
      "900eeaeb-0f27-4808-809c-4026a8fd7d5a"};
    const uint16_t handles[]={0x1d,0x24,0x85,0x8c,0x26,0x21,0x89};
    for(size_t i=0;i<7;++i) client.chars[ids[i]].handle=handles[i];
    client.chars[ids[5]].descriptor.handle=0x22;
    client.chars[ids[6]].descriptor.handle=0x8a;
    client.chars[std::to_string(0x2a19)].handle=0x58;
  }
  void tick(uint32_t amount=20) {
    test_now+=amount;
    d.loop();
    auto callbacks=std::move(client.deferred); client.deferred.clear();
    for (auto &callback:callbacks) callback();
  }
  void write_ack(uint16_t handle,int status_code=0,uint16_t conn=0) {
    esp_ble_gattc_cb_param_t param{};
    param.write.handle=handle;param.write.conn_id=conn?conn:client.connection;param.write.status=status_code;
    d.gattc_event_handler(ESP_GATTC_WRITE_CHAR_EVT,client.interface,&param);
  }
  void disconnect_event() {
    client.set_state(esp32_ble_tracker::ClientState::IDLE);
    esp_ble_gattc_cb_param_t param{}; param.disconnect.conn_id=client.connection;
    d.gattc_event_handler(ESP_GATTC_DISCONNECT_EVT,client.interface,&param);
  }
  void notify(int rail,uint8_t value) {
    esp_ble_gattc_cb_param_t param{};
    param.notify.handle=rail?0x89:0x21;param.notify.conn_id=client.connection;
    param.notify.value=&value;param.notify.value_len=1;
    d.gattc_event_handler(ESP_GATTC_NOTIFY_EVT,client.interface,&param);
  }
  void descriptor_ack(uint16_t handle,int status_code=0) {
    esp_ble_gattc_cb_param_t param{};
    param.write.handle=handle;param.write.conn_id=client.connection;param.write.status=status_code;
    d.gattc_event_handler(ESP_GATTC_WRITE_DESCR_EVT,client.interface,&param);
  }
  void notify_registered(int status_code=0) {
    esp_ble_gattc_cb_param_t param{};param.reg_for_notify.status=status_code;
    d.gattc_event_handler(ESP_GATTC_REG_FOR_NOTIFY_EVT,client.interface,&param);
  }
  void open_success() {
    // BLEClientBase promotes a cache-mode connection before node dispatch.
    client.set_state(esp32_ble_tracker::ClientState::ESTABLISHED);
    esp_ble_gattc_cb_param_t param{};param.open.status=ESP_GATT_OK;param.open.conn_id=client.connection;
    d.gattc_event_handler(ESP_GATTC_OPEN_EVT,client.interface,&param);
  }
  void service_ready() {
    client.set_state(esp32_ble_tracker::ClientState::ESTABLISHED);
    esp_ble_gattc_cb_param_t param{};param.search_cmpl.conn_id=client.connection;
    d.gattc_event_handler(ESP_GATTC_SEARCH_CMPL_EVT,client.interface,&param);
  }
  void read_ack(uint16_t handle,uint8_t value) {
    esp_ble_gattc_cb_param_t param{};param.read.conn_id=client.connection;
    param.read.handle=handle;param.read.value=&value;param.read.value_len=1;
    d.gattc_event_handler(ESP_GATTC_READ_CHAR_EVT,client.interface,&param);
  }
  void cancel_ack(int status_code=0) {
    esp_ble_gattc_cb_param_t param{};param.cancel_open.status=status_code;
    memcpy(param.cancel_open.remote_bda,client.address,6);
    d.gattc_event_handler(ESP_GATTC_CANCEL_OPEN_EVT,client.interface,&param);
  }
  void complete_battery(uint8_t first,uint8_t second,uint8_t third) {
    for(uint8_t value:{first,second,third}) {
      int budget=100;
      while(d.op_!=TestedDispatcher::OpKind::READ && budget-->0) tick(50);
      CHECK(d.op_==TestedDispatcher::OpKind::READ);CHECK(d.op_handle_==0x58);
      read_ack(0x58,value);
    }
  }
  void settle() { for(int i=0;i<3;++i) tick(); }
};

static void assert_last_write(uint16_t handle,std::initializer_list<uint8_t> bytes) {
  CHECK(!test_ble::requests.empty());
  const auto &r=test_ble::requests.back();
  CHECK(!r.read);CHECK(r.handle==handle);CHECK(r.value==std::vector<uint8_t>(bytes));
}

static void seed_handle_cache(Fixture &f);

static void test_existing_connection_all_commands() {
  for (int mode: {3,4,5,6,7}) {
    TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
    f.d.enqueue(mode); f.settle();
    if(mode==3) assert_last_write(0x1d,{0x16});
    if(mode==4) assert_last_write(0x1d,{0x12});
    if(mode==5) assert_last_write(0x85,{0x12});
    if(mode==6) assert_last_write(0x85,{0x16});
    if(mode==7) assert_last_write(0x26,{0x05});
    CHECK(f.client.connects==0);
    f.write_ack(test_ble::requests.back().handle);
    if(mode==5 || mode==7) {
      f.tick(110); f.tick();
      assert_last_write(0x1d,{uint8_t(mode==5?0x13:0x15)});
      f.write_ack(0x1d);
    }
    CHECK(f.d.delivered_);
  }
}

static void test_stack_shift_writes_fixed_targets_and_up_execute() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.notify(0,26);f.notify(1,71);
  f.d.enqueue(9);f.settle();
  CHECK(test_ble::requests.back().handle==0x24);
  CHECK(test_ble::requests.back().value==std::vector<uint8_t>({0x05}));
  const auto baseline=f.d.samples_;
  f.notify(0,27); // Early feedback before the final write ACK must remain fresh.
  f.write_ack(0x24);f.tick(70);assert_last_write(0x8c,{0x05});
  f.write_ack(0x8c);f.tick(70);assert_last_write(0x85,{0x18});
  f.write_ack(0x85);
  CHECK(f.d.delivered_);CHECK(f.d.command_samples_==baseline);
  f.notify(0,30);f.notify(1,75);f.tick(700);
  CHECK(!f.d.active_valid_);
  CHECK(f.status.state.find("target_verified")!=std::string::npos);
}

static void test_stack_shift_down_and_repeated_steps_are_queued() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.enqueue(10);f.d.enqueue(10);
  CHECK(f.d.queue_.size()==2); // Repeated relative steps are preserved, not coalesced.
  f.settle();assert_last_write(0x24,{0x05});
  f.write_ack(0x24);f.tick(70);assert_last_write(0x8c,{0x05});
  f.write_ack(0x8c);f.tick(70);assert_last_write(0x85,{0x19});
}

static void test_stack_shift_rejected_for_single_rail_and_bulk_requires_tdbu() {
  TestedDispatcher::reset_globals();Fixture single(1,false),dual(2,true);
  single.d.enqueue(9);single.d.enqueue(10);
  CHECK(single.d.queue_.empty());
  dual.d.enqueue(8,0,20,70);
  CHECK(dual.d.queue_.empty());
  CHECK(dual.status.state.find("paired_target_unverified")!=std::string::npos);
  GraberDispatcher::enqueue_bulk(9,{&dual.d,&single.d});
  CHECK(dual.d.queue_.empty());CHECK(single.d.queue_.empty());
}

static void test_bulk_stack_shift_waits_for_all_tdbu_participants() {
  TestedDispatcher::reset_globals();Fixture a(1,true),b(2,true);
  a.d.force_ready();b.d.force_ready();
  GraberDispatcher::enqueue_bulk(9,{&a.d,&b.d});
  a.tick();CHECK(!TestedDispatcher::bulk_operations_.empty());
  CHECK(!TestedDispatcher::bulk_operations_.front().released);
  b.tick();CHECK(TestedDispatcher::bulk_operations_.front().released);
  CHECK(a.d.active_.bulk_id==b.d.active_.bulk_id);
  CHECK(a.d.active_.mode==9&&b.d.active_.mode==9);
}

static void test_slider_snapshot_and_coalescing() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.enqueue(0,0,12);f.settle();assert_last_write(0x24,{12});
  const auto original=f.d.active_.id;
  f.d.enqueue(0,0,25);f.d.enqueue(0,1,88);f.d.enqueue(0,0,60);
  CHECK(f.d.active_.id==original);CHECK(f.d.active_.target==12);
  CHECK(f.d.queue_.size()==2);
  CHECK(f.d.queue_[0].rail==1);CHECK(f.d.queue_[0].target==88);
  CHECK(f.d.queue_[1].rail==0);CHECK(f.d.queue_[1].target==60);
  f.write_ack(0x24);f.tick(110);assert_last_write(0x1d,{0x14});
}

static void test_stop_drains_inflight_and_discards_old_command() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.enqueue(0,0,60);f.settle();assert_last_write(0x24,{60});
  f.d.enqueue(0,1,90);f.d.enqueue(1);
  auto before=test_ble::requests.size();f.tick(50);
  CHECK(test_ble::requests.size()==before);CHECK(f.d.active_.mode==0);
  CHECK(f.d.queue_.size()==1);CHECK(f.d.queue_[0].mode==1);
  f.write_ack(0x24);f.settle();assert_last_write(0x1d,{0x10});
  CHECK(f.d.active_.mode==1);CHECK(f.d.queue_.empty());
  f.write_ack(0x1d);f.tick(110);assert_last_write(0x85,{0x10});
  f.write_ack(0x85);CHECK(f.d.delivered_);
}

static void test_final_ack_disconnect_does_not_replay() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.enqueue(3);f.settle();assert_last_write(0x1d,{0x16});
  f.write_ack(0x1d);
  // No loop tick may be needed between ACK and disconnect to establish delivery.
  f.disconnect_event();f.tick(500);
  CHECK(!f.d.active_valid_);CHECK(f.client.connects==0);
  CHECK(f.status.state.find("unconfirmed")!=std::string::npos);
}

static void test_negative_ack_closes_before_retry() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.enqueue(0,1,88);f.settle();assert_last_write(0x8c,{88});
  f.write_ack(0x8c,5);CHECK(f.client.disconnects==1);
  CHECK(f.d.phase_==TestedDispatcher::Phase::CLOSING);
  auto before=test_ble::requests.size();f.tick(1000);
  CHECK(test_ble::requests.size()==before);CHECK(f.client.connects==0);
  // A late response must not advance the failed session.
  f.write_ack(0x8c);f.tick();CHECK(test_ble::requests.size()==before);
  f.disconnect_event();f.tick(400);CHECK(f.client.connects==1);
  CHECK(f.d.active_.attempts==2);
  CHECK(f.d.reconnect_at_==0);
}

static void test_three_sessions_have_independent_acks() {
  TestedDispatcher::reset_globals();Fixture a(1),b(2),c(3);
  for(auto *f:{&a,&b,&c}) {f->d.force_ready();f->d.enqueue(5);f->settle();}
  CHECK(test_ble::requests.size()==3);
  a.write_ack(0x85);
  CHECK(a.d.step_==1);CHECK(b.d.step_==0);CHECK(c.d.step_==0);
  b.write_ack(0x85,0,99);CHECK(b.d.step_==0);
  b.write_ack(0x85);c.write_ack(0x85);
  for(auto *f:{&a,&b,&c}) {f->tick(110);f->write_ack(0x1d);CHECK(f->d.delivered_);}
  CHECK(a.client.connects==0&&b.client.connects==0&&c.client.connects==0);
}

static void test_bulk_waits_until_every_participant_is_ready() {
  TestedDispatcher::reset_globals();Fixture a(1),b(2),c(3);
  a.d.force_ready();b.d.force_ready();c.d.force_ready();
  GraberDispatcher::enqueue_bulk(4,{&a.d,&b.d,&c.d});
  a.tick();b.tick();
  CHECK(test_ble::requests.empty());
  CHECK(a.d.active_.bulk_id==1&&b.d.active_.bulk_id==1);
  c.tick();
  CHECK(test_ble::requests.empty());
  CHECK(c.d.active_.bulk_id==1);
  CHECK(TestedDispatcher::bulk_operations_.size()==1);
  CHECK(TestedDispatcher::bulk_operations_.front().released);
  a.tick();b.tick();c.tick();
  a.tick();b.tick();c.tick();
  CHECK(test_ble::requests.size()==3);
  for(const auto &request:test_ble::requests) {
    CHECK(request.handle==0x1d);CHECK(request.value==std::vector<uint8_t>({0x12}));
  }
}

static void test_bulk_stage_failure_aborts_without_movement() {
  TestedDispatcher::reset_globals();Fixture a(1),b(2),c(3);
  a.d.force_ready();b.d.force_ready();
  GraberDispatcher::enqueue_bulk(5,{&a.d,&b.d,&c.d});
  a.tick();b.tick();c.tick();
  CHECK(test_ble::requests.empty());CHECK(c.client.connects==1);
  c.d.active_.attempts=3;c.d.fail_("test_stage_failure");
  CHECK(test_ble::requests.empty());
  CHECK(!a.d.active_valid_&&!b.d.active_valid_&&!c.d.active_valid_);
  CHECK(TestedDispatcher::bulk_operations_.empty());
  CHECK(a.status.state.find("bulk_aborted_before_dispatch")!=std::string::npos);
}

static void test_status_reports_phase_timings_and_cache_path() {
  TestedDispatcher::reset_globals();Fixture f;seed_handle_cache(f);
  f.d.enqueue(3);f.tick(25);f.open_success();f.tick(25);
  f.notify_registered();f.descriptor_ack(0x22);f.tick(25);
  f.notify_registered();f.descriptor_ack(0x8a);f.tick(25);
  f.write_ack(0x1d);f.tick(25);f.tick(25);f.write_ack(0x1d);
  CHECK(f.status.state.find("connect=")!=std::string::npos);
  CHECK(f.status.state.find("setup=")!=std::string::npos);
  CHECK(f.status.state.find("ack=")!=std::string::npos);
  CHECK(f.status.state.find("cache=hit")!=std::string::npos);
  CHECK(f.status.state.find("1234567")==std::string::npos);
}

static void test_services_and_cccd_before_auth() {
  TestedDispatcher::reset_globals();Fixture f;
  f.d.enqueue(3);f.tick();CHECK(f.client.connects==1);f.service_ready();f.tick();
  CHECK(f.d.op_==TestedDispatcher::OpKind::SUBSCRIBE);
  CHECK(test_ble::requests.empty());CHECK(!f.d.ready_);
  f.descriptor_ack(0x22);f.tick();CHECK(f.d.op_handle_==0x8a);
  CHECK(test_ble::requests.empty());f.descriptor_ack(0x8a);f.tick();
  assert_last_write(0x1d,{'1','2','3','4','5','6','7'});
  CHECK(!f.d.ready_);f.write_ack(0x1d);f.tick();CHECK(f.d.ready_);
  f.settle();assert_last_write(0x1d,{0x16});
}

static void test_connection_scheduler_moves_on_at_service_ready() {
  TestedDispatcher::reset_globals();Fixture a(1),b(2),c(3);
  for(auto *f:{&a,&b,&c}) f->d.enqueue(4);
  for(auto *f:{&a,&b,&c}) f->tick();
  CHECK(a.client.connects==1);CHECK(b.client.connects==0);CHECK(c.client.connects==0);
  a.service_ready();b.tick();c.tick();
  CHECK(b.client.connects==1);CHECK(c.client.connects==0);
  b.service_ready();c.tick();CHECK(c.client.connects==1);
}

static void test_successful_connection_stays_warm_for_thirty_seconds() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.battery_has_read_=true;f.d.battery_at_=millis();
  f.tick(29999);CHECK(f.client.disconnects==0);
  f.tick(11);CHECK(f.client.disconnects==1);
}

static void seed_handle_cache(Fixture &f) {
  const uint16_t values[] = {0x1d,0x24,0x85,0x8c,0x26,0x21,0x89,0x58};
  for(size_t i=0;i<f.d.cached_handles_.size();++i) f.d.cached_handles_[i]=values[i];
  f.d.cached_cccds_={0x22,0x8a};f.d.cache_valid_=true;
}

static void test_full_discovery_validates_cache_after_auth() {
  TestedDispatcher::reset_globals();Fixture f;
  f.d.enqueue(3);f.tick();CHECK(!f.d.cache_valid_);f.service_ready();f.tick();
  f.descriptor_ack(0x22);f.tick();f.descriptor_ack(0x8a);f.tick();
  CHECK(!f.d.cache_valid_);f.write_ack(0x1d);f.tick();
  CHECK(f.d.cache_valid_);CHECK(f.d.cached_handles_[0]==0x1d);
  CHECK(f.d.cached_cccds_[1]==0x8a);
}

static void test_cached_connect_skips_discovery_and_writes_learned_cccds() {
  TestedDispatcher::reset_globals();Fixture f;seed_handle_cache(f);
  f.d.enqueue(3);f.tick();
  CHECK(f.client.connection_type==esp32_ble_tracker::ConnectionType::V3_WITH_CACHE);
  f.open_success();CHECK(f.d.cache_in_use_);CHECK(f.d.phase_==TestedDispatcher::Phase::SETUP);
  f.tick();CHECK(f.d.op_==TestedDispatcher::OpKind::SUBSCRIBE);
  f.notify_registered();assert_last_write(0x22,{1,0});f.descriptor_ack(0x22);
  f.tick();f.notify_registered();assert_last_write(0x8a,{1,0});f.descriptor_ack(0x8a);
  f.tick();assert_last_write(0x1d,{'1','2','3','4','5','6','7'});f.write_ack(0x1d);f.tick();
  CHECK(f.d.ready_);CHECK(f.status.state.find("subscriptions_ready")!=std::string::npos);
}

static void test_cached_handle_failure_falls_back_to_discovery() {
  TestedDispatcher::reset_globals();Fixture f;seed_handle_cache(f);
  f.d.enqueue(3);f.tick();f.open_success();f.tick();f.notify_registered(5);
  CHECK(!f.d.cache_valid_);CHECK(f.client.connection_type==esp32_ble_tracker::ConnectionType::V1);
  CHECK(f.d.phase_==TestedDispatcher::Phase::CLOSING);
  f.disconnect_event();f.tick(400);
  CHECK(f.client.connects==2);CHECK(f.client.connection_type==esp32_ble_tracker::ConnectionType::V1);
  CHECK(f.d.active_.attempts==2);
}

static void test_fresh_position_required_and_range_checked() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.notify(0,60);f.notify(1,100);
  f.d.enqueue(0,0,60);f.settle();f.write_ack(0x24);
  f.tick(110);f.notify(0,60);f.write_ack(0x1d);
  CHECK(!f.d.desired_reached_()); // pre-execute-ACK sample is not confirmation
  const auto count=f.d.samples_[0];f.notify(0,255);CHECK(f.d.samples_[0]==count);
  f.notify(0,60);CHECK(f.d.desired_reached_());f.tick(700);
  CHECK(!f.d.active_valid_);CHECK(f.bottom.state==60);
}

static void test_battery_yields_to_other_motor_user_command() {
  TestedDispatcher::reset_globals();Fixture a(1),b(2);
  a.d.request_battery();b.d.enqueue(4);a.tick();
  CHECK(a.client.connects==0);CHECK(!a.d.active_valid_);
  b.tick();CHECK(b.client.connects==1);
}

static void test_retry_count_is_bounded() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.enqueue(0,0,60);
  for(int attempt=1;attempt<=3;++attempt) {
    if(attempt>1) {f.d.force_ready();f.tick();}
    f.settle();CHECK(f.d.active_.attempts==attempt);
    f.write_ack(0x24,5);
    f.disconnect_event();f.tick(400);
  }
  CHECK(!f.d.active_valid_);CHECK(f.client.connects==2);
}

static void test_connect_timeout_holds_scheduler_until_cancel_ack() {
  TestedDispatcher::reset_globals();Fixture a(1),b(2);
  a.d.enqueue(4);b.d.enqueue(4);a.tick();b.tick();
  a.tick(15001);CHECK(test_ble::cancellations==1);
  CHECK(a.d.phase_==TestedDispatcher::Phase::CLOSING);
  CHECK(a.d.connecting_==&a.d);b.tick();CHECK(b.client.connects==0);
  a.cancel_ack();CHECK(a.d.connecting_==nullptr);
  CHECK(a.client.state()==esp32_ble_tracker::ClientState::IDLE);
  b.tick();CHECK(b.client.connects==1);
}

static void test_late_open_during_cancel_is_closed() {
  TestedDispatcher::reset_globals();Fixture f;
  f.d.enqueue(4);f.tick();f.tick(15001);
  f.client.set_state(esp32_ble_tracker::ClientState::CONNECTED);
  esp_ble_gattc_cb_param_t param{};param.open.conn_id=f.client.connection;
  f.d.gattc_event_handler(ESP_GATTC_OPEN_EVT,f.client.interface,&param);
  CHECK(f.client.disconnects==1);CHECK(f.d.connecting_==&f.d);
  CHECK(test_ble::requests.empty());f.disconnect_event();
  CHECK(f.d.connecting_==nullptr);
}

static void test_cleanup_watchdog_does_not_reuse_live_slot() {
  TestedDispatcher::reset_globals();Fixture a(1),b(2);
  a.d.enqueue(4);b.d.enqueue(4);a.tick();a.tick(15001);
  a.tick(30001);CHECK(App.reboots==1);
  b.tick();CHECK(b.client.connects==0);
  CHECK(a.status.state.find("failed_ble_cleanup")!=std::string::npos);
}

static void test_failed_cccd_prevents_auth_or_movement() {
  TestedDispatcher::reset_globals();Fixture f;
  f.d.enqueue(4);f.tick();f.service_ready();f.tick();
  f.descriptor_ack(0x22,5);
  CHECK(f.client.disconnects==1);CHECK(!f.d.ready_);
  CHECK(test_ble::requests.empty());
}

static void test_command_write_timeout_and_late_response() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.enqueue(0,0,50);f.settle();f.tick(4001);
  CHECK(f.client.disconnects==1);CHECK(f.d.phase_==TestedDispatcher::Phase::CLOSING);
  const auto count=test_ble::requests.size();f.write_ack(0x24);f.tick();
  CHECK(test_ble::requests.size()==count);CHECK(!f.d.delivered_);
}

static void test_battery_invalid_read_never_overwrites_sensor() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.battery.publish_state(80);f.d.request_battery();f.settle();
  CHECK(test_ble::requests.back().read);CHECK(test_ble::requests.back().handle==0x58);
  f.read_ack(0x58,255);CHECK(f.battery.state==80);
  CHECK(!f.d.battery_pending_);CHECK(f.d.battery_has_read_);
  f.disconnect_event();f.tick(1000);CHECK(f.client.connects==0);
}

static void test_battery_zero_is_valid() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.request_battery();f.settle();f.complete_battery(0,0,0);
  CHECK(f.battery.state==0);CHECK(!f.d.active_valid_);CHECK(!f.d.battery_pending_);
}

static void test_one_streaming_rail_does_not_starve_other_read() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.enqueue(4);f.settle();f.write_ack(0x1d);
  f.tick(500); // May start first fallback bottom read, clear it if present.
  if(f.d.op_==TestedDispatcher::OpKind::READ) f.read_ack(f.d.op_handle_,25);
  for(int i=0;i<5;++i) {test_now+=300;f.notify(0,uint8_t(30+i));f.tick();}
  CHECK(f.d.op_==TestedDispatcher::OpKind::READ);
  CHECK(f.d.op_handle_==0x89);
}

static void test_stop_during_connect_preserves_handshake() {
  TestedDispatcher::reset_globals();Fixture f;
  f.d.enqueue(4);f.tick();f.d.enqueue(1);f.tick();
  CHECK(f.client.connects==1);CHECK(f.d.active_.mode==1);
  CHECK(f.d.phase_==TestedDispatcher::Phase::CONNECTING);
  f.service_ready();f.tick();f.descriptor_ack(0x22);f.tick();
  f.descriptor_ack(0x8a);f.tick();f.write_ack(0x1d);f.settle();
  assert_last_write(0x1d,{0x10});CHECK(f.client.connects==1);
}

static void test_long_uptime_does_not_disable_connect() {
  TestedDispatcher::reset_globals();Fixture f;
  test_now=0x80001000U; // About 25 days without a failure setting reconnect_at_.
  f.d.enqueue(4);f.tick();CHECK(f.client.connects==1);
}

static void test_clock_wrap_reads_oldest_rail() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  test_now=0xfffff000U;f.d.enqueue(4);f.settle();f.write_ack(0x1d);
  test_now=0xfffffe0cU;f.notify(0,50);
  test_now=100;f.notify(1,70);
  test_now=1300;f.tick();
  CHECK(f.d.op_==TestedDispatcher::OpKind::READ);CHECK(f.d.op_handle_==0x21);
}

static void test_missing_battery_waits_next_poll_interval() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.handles_[TestedDispatcher::Handle::BATTERY]=0;
  f.d.request_battery();f.settle();
  CHECK(!f.d.active_valid_);CHECK(!f.d.battery_pending_);CHECK(f.d.battery_has_read_);
  f.tick(4000);f.disconnect_event();f.tick(4000);
  CHECK(f.client.connects==0);
}

static void test_single_open_close_and_favorite_bytes() {
  for(int mode:{3,4,5}) {
    TestedDispatcher::reset_globals();Fixture f(1,false);f.d.force_ready();
    f.d.enqueue(mode);f.settle();
    if(mode==3) {assert_last_write(0x1d,{0x16});f.write_ack(0x1d);}
    else {
      assert_last_write(0x24,{uint8_t(mode==4?100:0)});
      f.write_ack(0x24);f.tick(110);assert_last_write(0x1d,{0x14});f.write_ack(0x1d);
      f.notify(0,uint8_t(mode==4?100:0));f.tick(700);
      CHECK(!f.d.active_valid_);
    }
    for(const auto &request:test_ble::requests) CHECK(request.handle!=0x85&&request.handle!=0x8c);
  }
}

static void test_single_rejects_top_and_dual_only_modes() {
  TestedDispatcher::reset_globals();Fixture f(1,false);
  f.d.enqueue(0,1,50);f.d.enqueue(6);f.d.enqueue(7);f.d.enqueue(8,0,0,50);
  CHECK(f.d.queue_.empty());f.tick();CHECK(f.client.connects==0);
}

static void test_single_discovers_without_any_top_characteristics() {
  TestedDispatcher::reset_globals();Fixture f(1,false);
  for(const auto *uuid:{"bb27521d-2fd4-4187-af9f-5d0780238d7e","a506fbb1-9777-4e43-aa13-67d1bab6c0a1",
                       "e3fc1ebf-e615-44f2-b1dd-b6b95266a453","900eeaeb-0f27-4808-809c-4026a8fd7d5a"})
    f.client.chars.erase(uuid);
  f.d.enqueue(3);f.tick();f.service_ready();f.tick();CHECK(f.d.op_handle_==0x22);
  f.descriptor_ack(0x22);f.tick();assert_last_write(0x1d,{'1','2','3','4','5','6','7'});
  f.write_ack(0x1d);f.settle();assert_last_write(0x1d,{0x16});
  CHECK(f.d.handles_[TestedDispatcher::Handle::EXECUTE]==0);
  CHECK(f.d.handles_[TestedDispatcher::Handle::TOP_POSITION]==0);
  CHECK(f.d.handles_[TestedDispatcher::Handle::TOP_TARGET]==0);
}

static void test_single_stop_with_optional_execute() {
  for(bool execute_exists:{false,true}) {
    TestedDispatcher::reset_globals();Fixture f(1,false);f.d.force_ready();
    if(!execute_exists) f.d.handles_[TestedDispatcher::Handle::EXECUTE]=0;
    f.d.enqueue(1);f.settle();assert_last_write(0x1d,{0x10});f.write_ack(0x1d);
    if(execute_exists) {
      f.tick(110);assert_last_write(0x85,{0x10});f.write_ack(0x85);
    }
    CHECK(f.d.delivered_);
    f.notify(0,42);f.tick(2100);CHECK(!f.d.active_valid_);
    CHECK(f.status.state.find("stop_acknowledged_position_stable")!=std::string::npos);
  }
}

static void test_single_favorite_only_needs_bottom_feedback() {
  TestedDispatcher::reset_globals();Fixture f(1,false);f.d.force_ready();
  f.notify(0,0);f.d.enqueue(3);f.settle();f.write_ack(0x1d);
  f.notify(0,50);f.tick(2100);
  CHECK(!f.d.active_valid_);CHECK(f.status.state.find("favorite_acknowledged_position_stable")!=std::string::npos);
  CHECK(f.d.samples_[1]==0);
}

static void test_single_no_top_reads_or_notifications() {
  TestedDispatcher::reset_globals();Fixture f(1,false);f.d.force_ready();
  f.d.enqueue(3);f.settle();f.write_ack(0x1d);f.notify(1,90);
  CHECK(f.d.samples_[1]==0);CHECK(!std::isfinite(f.top.state));
  for(int i=0;i<4;++i) {f.tick(1300);CHECK(f.d.op_handle_==0x21);f.read_ack(0x21,50);}
  for(const auto &request:test_ble::requests) CHECK(request.handle!=0x89);
}

static void test_boot_battery_authenticates_reads_and_never_moves() {
  for(bool dual:{false,true}) {
    TestedDispatcher::reset_globals();Fixture f(1,dual);f.tick(30001);f.service_ready();f.tick();
    f.descriptor_ack(0x22);f.tick();
    if(dual) {f.descriptor_ack(0x8a);f.tick();}
    f.write_ack(0x1d);f.settle();CHECK(f.d.active_.mode==2);
    CHECK(test_ble::requests.back().read);CHECK(test_ble::requests.back().handle==0x58);
    f.complete_battery(75,75,75);CHECK(f.battery.state==75);CHECK(!f.d.active_valid_);
    for(const auto &request:test_ble::requests) {
      if(!request.read) {CHECK(request.handle==0x1d);CHECK(request.value.size()==7);}
    }
  }
}

static void test_single_no_motion_does_not_invent_completion() {
  TestedDispatcher::reset_globals();Fixture f(1,false);f.d.force_ready();
  f.notify(0,50);f.d.enqueue(3);f.settle();f.write_ack(0x1d);
  f.notify(0,50);f.tick(2100);
  CHECK(f.d.active_valid_);CHECK(f.status.state.find("favorite_acknowledged_position_stable")==std::string::npos);
  if(f.d.op_==TestedDispatcher::OpKind::READ) f.read_ack(0x21,50);
  f.tick(90001);CHECK(!f.d.active_valid_);
  CHECK(f.status.state.find("completion_unconfirmed_timeout")!=std::string::npos);
}

static void test_battery_transient_zero_then_stable_full() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.battery.publish_state(80);f.d.request_battery();f.settle();
  f.complete_battery(0,100,100);
  CHECK(f.battery.history==std::vector<float>({80,100}));
  CHECK(f.status.state.find("battery_read_confirmed")!=std::string::npos);
}

static void test_battery_stable_zero_after_initial_full() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.battery.publish_state(80);f.d.request_battery();f.settle();
  f.complete_battery(100,0,0);
  CHECK(f.battery.history==std::vector<float>({80,0}));
}

static void test_battery_unstable_keeps_previous_value() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.battery.publish_state(80);f.d.request_battery();f.settle();
  f.complete_battery(0,100,0);
  CHECK(f.battery.history==std::vector<float>({80}));CHECK(!f.d.active_valid_);
  CHECK(f.status.state.find("battery_unstable_preserving_previous")!=std::string::npos);
}

static void test_battery_three_read_timing() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.request_battery();f.settle();
  const auto start=f.d.battery_started_at_;
  CHECK(test_ble::requests.size()==1);f.read_ack(0x58,75);
  test_now=start+998;f.tick(1);CHECK(test_ble::requests.size()==1);
  f.tick(1);CHECK(test_ble::requests.size()==2);f.read_ack(0x58,75);
  test_now=start+2998;f.tick(1);CHECK(test_ble::requests.size()==2);
  f.tick(1);CHECK(test_ble::requests.size()==3);f.read_ack(0x58,75);
  CHECK(f.battery.state==75);CHECK(!f.d.active_valid_);
}

static void test_battery_invalid_later_sample_keeps_previous() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.battery.publish_state(80);f.d.request_battery();f.settle();
  f.read_ack(0x58,100);f.tick(1000);f.read_ack(0x58,255);
  CHECK(f.battery.history==std::vector<float>({80}));CHECK(!f.d.active_valid_);
}

static void test_battery_error_preserves_previous() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.battery.publish_state(80);f.d.request_battery();f.settle();
  esp_ble_gattc_cb_param_t param{};param.read.conn_id=f.client.connection;
  param.read.handle=0x58;param.read.status=5;
  f.d.gattc_event_handler(ESP_GATTC_READ_CHAR_EVT,f.client.interface,&param);
  CHECK(f.battery.history==std::vector<float>({80}));CHECK(!f.d.active_valid_);
}

static void test_battery_yields_after_read_to_user_command() {
  TestedDispatcher::reset_globals();Fixture f(1,false);f.d.force_ready();
  f.battery.publish_state(80);f.d.request_battery();f.settle();
  f.d.enqueue(0,0,60);f.tick(100);CHECK(f.d.active_.mode==2);
  CHECK(test_ble::requests.size()==1);f.read_ack(0x58,0);f.settle();
  CHECK(f.d.active_.mode==0);assert_last_write(0x24,{60});
  CHECK(f.d.battery_pending_);CHECK(f.d.battery_sample_count_==0);
  // An old battery read cannot satisfy the motor's target write or publish a value.
  f.read_ack(0x58,100);CHECK(f.d.op_==TestedDispatcher::OpKind::WRITE);
  CHECK(f.battery.history==std::vector<float>({80}));
}

static void test_battery_yields_after_read_to_stop() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.request_battery();f.settle();f.d.enqueue(1);f.tick(100);
  CHECK(f.d.active_.mode==2);f.read_ack(0x58,75);f.settle();
  CHECK(f.d.active_.mode==1);assert_last_write(0x1d,{0x10});
  CHECK(f.d.battery_pending_);CHECK(!std::isfinite(f.battery.state));
}

static void test_refresh_all_batteries_requests_every_motor() {
  TestedDispatcher::reset_globals();Fixture a(1),b(2,false),c(3);
  GraberDispatcher::request_all_batteries();
  CHECK(a.d.battery_pending_&&b.d.battery_pending_&&c.d.battery_pending_);
  CHECK(test_ble::requests.empty());
}

static void test_battery_log_contains_read_evidence_without_auth() {
  TestedDispatcher::reset_globals();Fixture f;f.d.force_ready();
  f.d.request_battery();f.settle();f.complete_battery(0,100,100);
  unsigned responses=0;
  for(const auto &message:test_log::messages) {
    CHECK(message.find("1234567")==std::string::npos);
    if(message.find("battery response")!=std::string::npos) {
      ++responses;
      CHECK(message.find("conn=1 handle=0x0058 status=0 len=1 raw=")!=std::string::npos);
      CHECK(message.find("connected_ms=")!=std::string::npos);
      CHECK(message.find("poll_ms=")!=std::string::npos);
    }
  }
  CHECK(responses==3);
}

int main() {
  struct Case { const char *name; void (*run)(); };
  const Case cases[]={
    {"existing connection handles favorite/open/close/presets",test_existing_connection_all_commands},
    {"stack Up uses fixed targets and fresh two-rail feedback",test_stack_shift_writes_fixed_targets_and_up_execute},
    {"stack Down uses fixed targets and repeated steps queue",test_stack_shift_down_and_repeated_steps_are_queued},
    {"stack shifts reject single-rail and mixed bulk participants",test_stack_shift_rejected_for_single_rail_and_bulk_requires_tdbu},
    {"bulk stack shift releases only after all TDBU participants are ready",test_bulk_stack_shift_waits_for_all_tdbu_participants},
    {"slider snapshots and queued target coalescing",test_slider_snapshot_and_coalescing},
    {"Stop drains write and cancels pending movement",test_stop_drains_inflight_and_discards_old_command},
    {"final ACK then disconnect never replays",test_final_ack_disconnect_does_not_replay},
    {"negative ACK closes before retry; late ACK ignored",test_negative_ack_closes_before_retry},
    {"three sessions preserve independent ACKs",test_three_sessions_have_independent_acks},
    {"bulk waits for every authenticated participant",test_bulk_waits_until_every_participant_is_ready},
    {"bulk staging failure aborts before movement",test_bulk_stage_failure_aborts_without_movement},
    {"status reports phase timings and cache path",test_status_reports_phase_timings_and_cache_path},
    {"CCCDs acknowledged before auth and movement",test_services_and_cccd_before_auth},
    {"connection scheduler releases at service discovery",test_connection_scheduler_moves_on_at_service_ready},
    {"successful connection remains warm for thirty seconds",test_successful_connection_stays_warm_for_thirty_seconds},
    {"full discovery caches handles only after auth validation",test_full_discovery_validates_cache_after_auth},
    {"cached reconnect skips discovery and writes learned CCCDs",test_cached_connect_skips_discovery_and_writes_learned_cccds},
    {"cached handle failure falls back to full discovery",test_cached_handle_failure_falls_back_to_discovery},
    {"fresh target feedback and invalid samples",test_fresh_position_required_and_range_checked},
    {"battery work yields to another motor command",test_battery_yields_to_other_motor_user_command},
    {"retries bounded per immutable command",test_retry_count_is_bounded},
    {"connect cancellation holds scheduler until ACK",test_connect_timeout_holds_scheduler_until_cancel_ack},
    {"late OPEN while canceling closes real connection",test_late_open_during_cancel_is_closed},
    {"wedged cleanup watchdog never reuses live slot",test_cleanup_watchdog_does_not_reuse_live_slot},
    {"failed CCCD prevents auth and motor writes",test_failed_cccd_prevents_auth_or_movement},
    {"write timeout ignores late response",test_command_write_timeout_and_late_response},
    {"invalid battery read preserves prior valid value",test_battery_invalid_read_never_overwrites_sensor},
    {"battery zero is a valid reported value",test_battery_zero_is_valid},
    {"streaming rail cannot starve other rail read",test_one_streaming_rail_does_not_starve_other_read},
    {"Stop during connect preserves existing handshake",test_stop_during_connect_preserves_handshake},
    {"long uptime does not disable new connects",test_long_uptime_does_not_disable_connect},
    {"millis wrap chooses oldest rail for feedback read",test_clock_wrap_reads_oldest_rail},
    {"missing battery defers until next poll interval",test_missing_battery_waits_next_poll_interval},
    {"single open/close/favorite match isolated captures",test_single_open_close_and_favorite_bytes},
    {"single rejects top rail and dual-only presets",test_single_rejects_top_and_dual_only_modes},
    {"single discovery needs no top characteristics",test_single_discovers_without_any_top_characteristics},
    {"single Stop supports optional Execute characteristic",test_single_stop_with_optional_execute},
    {"single favorite verified from bottom feedback only",test_single_favorite_only_needs_bottom_feedback},
    {"single ignores top notifications and never reads top",test_single_no_top_reads_or_notifications},
    {"boot battery read never writes movement commands",test_boot_battery_authenticates_reads_and_never_moves},
    {"single no-motion favorite remains unconfirmed",test_single_no_motion_does_not_invent_completion},
    {"battery transient zero followed by two full reads",test_battery_transient_zero_then_stable_full},
    {"battery two zero reads after initial full is valid",test_battery_stable_zero_after_initial_full},
    {"unstable battery keeps previous published value",test_battery_unstable_keeps_previous_value},
    {"battery reads follow minimum 0/1/3 second schedule",test_battery_three_read_timing},
    {"invalid later battery sample preserves previous",test_battery_invalid_later_sample_keeps_previous},
    {"battery read error preserves previous",test_battery_error_preserves_previous},
    {"battery yields after inflight read to slider command",test_battery_yields_after_read_to_user_command},
    {"battery yields after inflight read to Stop",test_battery_yields_after_read_to_stop},
    {"refresh all batteries marks every motor pending",test_refresh_all_batteries_requests_every_motor},
    {"battery logs contain raw read evidence without auth",test_battery_log_contains_read_evidence_without_auth},
  };
  int failures=0;
  for(const auto &test:cases) {
    try {test.run();std::cout<<"PASS "<<test.name<<'\n';}
    catch(const std::exception &e) {++failures;std::cout<<"FAIL "<<test.name<<": "<<e.what()<<'\n';}
  }
  std::cout<<"Cases: "<<sizeof(cases)/sizeof(cases[0])<<", failures: "<<failures<<'\n';
  return failures?1:0;
}
