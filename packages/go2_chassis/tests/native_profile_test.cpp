#include <cassert>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <thread>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "go2_chassis/daemon_core.hpp"
#include "go2_chassis/native_profile.hpp"
#include "go2_chassis/native_motion_evidence.hpp"
#include "go2_chassis/rpc_response_guard.hpp"
#include "go2_chassis/seqpacket_client.hpp"

using namespace go2_chassis;

class FakeClient final : public ISportClient {
 public:
  bool Initialize(const std::string &, std::string *) override { return true; }
  std::int32_t PrepareArm() override { return 0; }
  std::int32_t ClassicWalk(bool) override { return 0; }
  std::int32_t Move(float x, float y, float z) override {
    vx=x; vy=y; wz=z; ++moves; return move_result;
  }
  std::int32_t StopMove() override { ++stops; return 0; }
  std::int32_t SportActionCall(SportAction action) override {
    last=action; ++postures; return sport_action_result;
  }
  bool FreshSportState(std::uint64_t) const override { return fresh; }
  bool PostureComplete(SportAction action, std::uint64_t,
                       std::uint64_t started_ns) const override {
    return fresh && state_received_ns>started_ns &&
           NativePostureModeReached(action,mode);
  }
  float vx{0}, vy{0}, wz{0};
  int moves{0}, stops{0}, postures{0};
  std::int32_t move_result{0};
  std::int32_t sport_action_result{0};
  bool fresh{true};
  std::uint8_t mode{255U};
  std::uint64_t state_received_ns{0};
  SportAction last{SportAction::kHello};
};

DaemonConfig NativeConfig() {
  DaemonConfig config;
  config.allow_motion=true; config.allow_reverse=true; config.native_postures_only=true;
  config.max_vx=static_cast<float>(kNativeMaxVx);
  config.max_vy=static_cast<float>(kNativeMaxVy);
  config.max_wz=static_cast<float>(kNativeMaxWz);
  config.max_motion_ns=kNativeMaxMotionMs*1'000'000ULL;
  return config;
}

void SignedMotionAndPostures() {
  constexpr std::uint64_t now=1'000'000'000ULL;
  FakeClient client;
  DaemonCore core(NativeConfig(),client);
  assert(client.postures==0); // startup never dispatches posture
  assert(core.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now).code==0);
  assert(core.Handle(MakeCommand(CommandOp::kMove,2,now+1,100'000'000,-0.04F,0.02F),now+1).code==0);
  assert(client.vx<0 && client.vy>0);
  assert(core.Handle(MakeCommand(CommandOp::kStop,3,now+2,100'000'000),now+2).code==0);
  assert(core.Handle(MakeSportActionCommand(SportAction::kStandDown,4,now+3,100'000'000),now+3).code==0);
  assert(client.last==SportAction::kStandDown && client.postures==1);
  const auto overlap=core.Handle(MakeCommand(CommandOp::kMove,5,now+4,100'000'000,0.01F),now+4);
  assert(overlap.code!=0 && core.faulted() && client.stops>=2);
  core.OnDisconnect();
  assert(client.postures==1); // shutdown never dispatches posture
  for (const auto action : {SportAction::kStandUp,SportAction::kStandDown}) {
    FakeClient fake;
    DaemonCore daemon(NativeConfig(),fake);
    assert(daemon.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now).code==0);
    assert(daemon.Handle(MakeSportActionCommand(action,2,now+1,100'000'000),now+1).code==0);
    assert(fake.postures==1 && fake.last==action);
    assert(daemon.CheckWatchdog(now+400'000'000));
    assert(fake.stops>0 && !daemon.armed());
  }
  FakeClient disallowed;
  DaemonCore daemon(NativeConfig(),disallowed);
  daemon.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now);
  assert(daemon.Handle(MakeSportActionCommand(SportAction::kHello,2,now+1,100'000'000),now+1).code!=0);
  assert(disallowed.postures==0);
  FakeClient stale;
  DaemonCore stale_daemon(NativeConfig(),stale);
  stale_daemon.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now);
  stale.fresh=false;
  assert(stale_daemon.Handle(
      MakeSportActionCommand(SportAction::kStandUp,2,now+1,100'000'000),now+1).code!=0);
  assert(stale.postures==0 && stale_daemon.faulted());
  FakeClient idle;
  DaemonCore idle_daemon(NativeConfig(),idle);
  assert(idle_daemon.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now).code==0);
  idle.fresh=false;
  assert(idle_daemon.CheckWatchdog(now+1));
  assert(!idle_daemon.armed() && idle.stops>0);
  FakeClient no_state;
  no_state.fresh=false;
  DaemonCore no_state_daemon(NativeConfig(),no_state);
  assert(no_state_daemon.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now).code!=0);
  assert(!no_state_daemon.armed() && no_state.moves==0 && no_state.postures==0);
  for (std::uint8_t mode=0; mode<20; ++mode) {
    assert(NativeMovementMode(mode)==(mode==1 || mode==3));
  }
}

void GuardRetainsBothSigns() {
  GuardConfig config;
  config.allow_motion=true; config.allow_reverse=true; config.allowed_modes={5};
  config.max_vx=kNativeMaxVx; config.max_vy=kNativeMaxVy; config.max_wz=kNativeMaxWz;
  SafetyGuard guard(config);
  guard.UpdateRobotState(10,5,true); guard.UpdateCommand(10,{});
  assert(guard.RequestArm(10,nullptr));
  for (int i=1;i<=30;++i) {
    const double now=10+i*0.02;
    guard.UpdateRobotState(now,5,true); guard.UpdateCommand(now,{});
    guard.Tick(now,0.02);
  }
  guard.UpdateRobotState(10.62,5,true);
  guard.UpdateCommand(10.62,{-0.04,-0.02,-0.1});
  const auto decision=guard.Tick(10.62,0.02);
  assert(decision.velocity.vx<0 && decision.velocity.vy<0 && decision.velocity.wz<0);
  assert(!NativeVelocityAllowed(0.01,0,0,kNativeMaxVx,kNativeMaxVy,kNativeMaxWz,true));
  assert(NativeVelocityAllowed(0,0,0,kNativeMaxVx,kNativeMaxVy,kNativeMaxWz,true));
}

void DelayedTicksUseElapsedSlewWithTheExistingStepCeiling() {
  GuardConfig config;
  config.allow_motion=true;
  config.allow_reverse=true;
  config.allowed_modes={1};
  config.max_vx=kNativeMaxVx;
  config.max_vy=kNativeMaxVy;
  config.max_wz=kNativeMaxWz;
  SafetyGuard guard(config);
  guard.UpdateRobotState(20,1,true);
  guard.UpdateCommand(20,{});
  assert(guard.RequestArm(20,nullptr));
  guard.UpdateRobotState(20.5,1,true);
  guard.UpdateCommand(20.5,{});
  assert(guard.Tick(20.5,0.0).state==GuardState::kArmed);
  const auto delayed_tick=[&guard](double now) {
    guard.UpdateRobotState(now,1,true);
    guard.UpdateCommand(now,{kNativeMaxVx,0,kNativeMaxWz});
    const auto decision=guard.Tick(now,0.0);
    assert(decision.action==GuardAction::kMove);
    assert(decision.state==GuardState::kArmed);
    return decision.velocity;
  };
  // An 80 ms SDK/evidence delay must use 80 ms, not the nominal 20 ms.
  const auto after_80ms=delayed_tick(20.58);
  assert(std::fabs(after_80ms.vx-0.024)<1e-10);
  assert(std::fabs(after_80ms.wz-0.064)<1e-10);
  // A 150 ms delay is still limited to the existing 100 ms slew step.
  const auto after_150ms=delayed_tick(20.73);
  assert(std::fabs(after_150ms.vx-after_80ms.vx-0.030)<1e-10);
  assert(std::fabs(after_150ms.wz-after_80ms.wz-0.080)<1e-10);
  // Shorter follow-up ticks also use their actual interval.
  const auto after_10ms=delayed_tick(20.74);
  assert(std::fabs(after_10ms.vx-after_150ms.vx-0.003)<1e-10);
  assert(std::fabs(after_10ms.wz-after_150ms.wz-0.008)<1e-10);
}

void IpcPreservesPostureIds() {
  char temporary[]="/tmp/go2-native-ipc-XXXXXX";
  const auto directory=::mkdtemp(temporary);
  assert(directory!=nullptr);
  const std::string path=std::string(directory)+"/test.sock";
  const int server=::socket(AF_UNIX,SOCK_SEQPACKET,0);
  assert(server>=0);
  sockaddr_un address{}; address.sun_family=AF_UNIX;
  std::strncpy(address.sun_path,path.c_str(),sizeof(address.sun_path)-1);
  assert(::bind(server,reinterpret_cast<sockaddr *>(&address),sizeof(address))==0);
  assert(::listen(server,1)==0);
  std::thread responder([server]() {
    const int peer=::accept(server,nullptr,nullptr); assert(peer>=0);
    for (const auto operation : {CommandOp::kSportAction,CommandOp::kFinishPosture}) {
      for (const auto action : {SportAction::kStandUp,SportAction::kStandDown,
                               SportAction::kBalanceStand}) {
        CommandPacket command{};
        assert(::recv(peer,&command,sizeof(command),0)==static_cast<ssize_t>(sizeof(command)));
        assert(HasValidChecksum(command));
        assert(command.operation==static_cast<std::uint8_t>(operation));
        assert(command.reserved8==static_cast<std::uint8_t>(action));
        const auto reply=MakeReply(command.sequence,ReplyCode::kOk,true,false);
        assert(::send(peer,&reply,sizeof(reply),0)==static_cast<ssize_t>(sizeof(reply)));
      }
    }
    ::close(peer);
  });
  SeqpacketClient client(path);
  std::int32_t code=0; std::string error;
  for (const auto operation : {CommandOp::kSportAction,CommandOp::kFinishPosture}) {
    for (const auto action : {SportAction::kStandUp,SportAction::kStandDown,
                             SportAction::kBalanceStand}) {
      assert(client.Exchange(operation,{},&code,&error,action));
      assert(client.last_reply_received() && code==0);
    }
  }
  responder.join(); ::close(server); ::unlink(path.c_str()); ::rmdir(directory);
}

void PersistentSessionStopsBetweenActions() {
  constexpr std::uint64_t now=1'000'000'000ULL;
  FakeClient client;
  DaemonCore core(NativeConfig(),client);
  std::uint64_t sequence=1;
  assert(core.Handle(MakeCommand(CommandOp::kArm,sequence++,now,100'000'000),now).code==0);
  assert(core.Handle(MakeCommand(CommandOp::kMove,sequence++,now+1,100'000'000,0.02F),now+1).code==0);
  assert(core.Handle(MakeCommand(CommandOp::kStop,sequence++,now+2,100'000'000),now+2).code==0);
  assert(core.armed() && !core.moving());
  for (std::uint64_t i=1; i<=65; ++i) {
    const auto current=now+i*100'000'000ULL;
    assert(core.Handle(MakeCommand(CommandOp::kPing,sequence++,current,100'000'000),
                       current).code==0);
    assert(!core.CheckWatchdog(current));
  }
  const auto later=now+6'600'000'000ULL;
  assert(core.Handle(MakeCommand(CommandOp::kMove,sequence++,later,100'000'000,-0.02F),
                     later).code==0);
  assert(client.moves==2 && core.armed());
  assert(core.Handle(MakeCommand(CommandOp::kStop,sequence++,later+1,100'000'000),
                     later+1).code==0);
  assert(core.Handle(MakeCommand(CommandOp::kDisarm,sequence++,later+2,100'000'000),
                     later+2).code==0);
  assert(!core.armed());
  assert(!NativeCommandAfterStop(0,100));
  assert(!NativeCommandAfterStop(99,100));
  assert(!NativeCommandAfterStop(100,100));
  assert(NativeCommandAfterStop(101,100));
}

void HalfSecondMoveAndExplicitStopHaveNoResidualDispatch() {
  // Exercise the guard -> private IPC packet -> fake SDK path at native
  // control frequency, for every signed axis. No DDS or SDK is initialized.
  for (const Velocity requested : {
           Velocity{0.05,0,0}, Velocity{-0.05,0,0},
           Velocity{0,0.04,0}, Velocity{0,-0.04,0},
           Velocity{0,0,0.10}, Velocity{0,0,-0.10}}) {
    GuardConfig config;
    config.allow_motion=true;
    config.allow_reverse=true;
    config.allowed_modes={1,3,5,6};
    config.max_vx=kNativeMaxVx;
    config.max_vy=kNativeMaxVy;
    config.max_wz=kNativeMaxWz;
    config.command_timeout_sec=0.20;
    SafetyGuard guard(config);
    FakeClient client;
    DaemonCore core(NativeConfig(),client);
    std::uint64_t sequence=1;
    const auto tick_ns=[](double seconds) {
      return static_cast<std::uint64_t>(seconds*1'000'000'000.0);
    };
    double current=10;
    guard.UpdateRobotState(current,1,true);
    guard.UpdateCommand(current,{});
    assert(guard.RequestArm(current,nullptr));
    for (int step=1; step<=30; ++step) {
      current=10+step*0.02;
      guard.UpdateRobotState(current,1,true);
      guard.UpdateCommand(current,{});
      guard.Tick(current,0.02);
    }
    assert(guard.state()==GuardState::kArmed);
    auto now_ns=tick_ns(current);
    assert(core.Handle(MakeCommand(CommandOp::kArm,sequence++,now_ns,100'000'000),
                       now_ns).code==0);
    NativeMotionEvidence evidence;
    for (int step=1; step<=25; ++step) {
      current=10.6+step*0.02;
      now_ns=tick_ns(current);
      guard.UpdateRobotState(current,1,true);
      guard.UpdateCommand(current,requested);
      const auto decision=guard.Tick(current,0.02);
      assert(decision.action==GuardAction::kMove);
      evidence.MoveAttempt(decision.velocity,now_ns);
      const auto reply=core.Handle(
          MakeCommand(CommandOp::kMove,sequence++,now_ns,100'000'000,
                      decision.velocity.vx,decision.velocity.vy,decision.velocity.wz),
          now_ns);
      evidence.MoveReply(reply.code==0,true,reply.code,now_ns);
      assert(reply.code==0 && core.armed() && !core.faulted());
      assert((reply.reserved16 & kReplyNativeMoveRemoteAck)!=0U);
    }
    assert(client.moves==25 && evidence.sdk_move_dispatch_count==25);
    assert(std::fabs(client.vx-requested.vx)<1e-6);
    assert(std::fabs(client.vy-requested.vy)<1e-6);
    assert(std::fabs(client.wz-requested.wz)<1e-6);
    assert(client.postures==0);

    // This is the explicit /finish sequence. Resetting only the target (the
    // previous implementation) leaves a .044 m/s Move after a .05 m/s Stop.
    guard.RequestStop(current);
    assert(core.Handle(MakeCommand(CommandOp::kStop,sequence++,now_ns,100'000'000),
                       now_ns).code==0);
    const auto move_count_after_stop=client.moves;
    for (int step=1; step<=30; ++step) {
      const double stopped_now=current+step*0.02;
      now_ns=tick_ns(stopped_now);
      guard.UpdateRobotState(stopped_now,1,true);
      guard.UpdateCommand(stopped_now,{});
      const auto decision=guard.Tick(stopped_now,0.02);
      assert(decision.action==GuardAction::kStop);
      assert(decision.velocity.vx==0 && decision.velocity.vy==0 &&
             decision.velocity.wz==0);
      assert(core.Handle(MakeCommand(CommandOp::kPing,sequence++,now_ns,100'000'000),
                         now_ns).code==0);
      assert(!core.CheckWatchdog(now_ns));
    }
    assert(client.moves==move_count_after_stop);
    assert(evidence.sdk_move_dispatch_count==25); // Ping never proves movement
    assert(core.armed() && guard.state()==GuardState::kArmed);

    const double next=current+0.62;
    guard.UpdateRobotState(next,1,true);
    guard.UpdateCommand(next,requested);
    const auto restarted=guard.Tick(next,0.02);
    assert(restarted.action==GuardAction::kMove);
    assert(std::fabs(restarted.velocity.vx)<=0.0060001);
    assert(std::fabs(restarted.velocity.vy)<=0.0060001);
    assert(std::fabs(restarted.velocity.wz)<=0.0160001);
    assert(!NativeCommandAfterStop(150,200)); // sent during the stop RPC
    assert(!NativeCommandAfterStop(200,200));
    assert(NativeCommandAfterStop(201,200));

    // Explicit stop is not a watchdog exemption or fault-reset operation.
    guard.RequestStop(next);
    assert(guard.Tick(next+0.21,0.02).state==GuardState::kFault);
    guard.RequestStop(next+0.21);
    assert(guard.state()==GuardState::kFault);
  }
}

void NativeMoveRequiresMatchingResponseAndPropagatesRejection() {
  assert(!NativeMoveReplyAcknowledged(0,0)); // old one-way daemon's "success"
  assert(NativeMoveReplyAcknowledged(0,kReplyNativeMoveRemoteAck));
  assert(!NativeMoveReplyAcknowledged(-1,kReplyNativeMoveRemoteAck));
  const std::string parameter=R"({"x":0.05,"y":0.0,"z":0.0})";
  const RpcCallExpectation native{1008,0,false,false,0,parameter};
  const RpcRequestEvidence request{true,false,12345,1008,0,false,0,parameter};
  const RpcResponseEvidence response{true,false,12345,1008,0};
  assert(ValidateRpcCallEvidence(native,request,response,0).ok());
  assert(ValidateRpcCallEvidence(native,request,{},0).code==
         RpcEvidenceCode::kResponseMissing);
  auto one_way=request;
  one_way.noreply=true;
  assert(ValidateRpcCallEvidence(native,one_way,response,0).code==
         RpcEvidenceCode::kRequestMismatch);
  auto wrong_identity=response;
  ++wrong_identity.identity_id;
  assert(ValidateRpcCallEvidence(native,request,wrong_identity,0).code==
         RpcEvidenceCode::kResponseMismatch);
  auto rejected=response;
  rejected.status_code=-1;
  assert(RpcEvidenceReturnCode(ValidateRpcCallEvidence(native,request,rejected,0))==-1);
  assert(RpcEvidenceReturnCode(ValidateRpcCallEvidence(native,request,{},3104))==3104);
  const RpcCallExpectation legacy{1008,0,false,true,0,parameter};
  assert(ValidateRpcCallEvidence(legacy,one_way,{},0).ok());

  constexpr std::uint64_t now=1'000'000'000ULL;
  for (const std::int32_t result : {-1,3104,-4107,7001}) {
    FakeClient client;
    client.move_result=result;
    DaemonCore native_core(NativeConfig(),client);
    assert(native_core.Handle(
        MakeCommand(CommandOp::kArm,1,now,100'000'000),now).code==0);
    const auto reply=native_core.Handle(
        MakeCommand(CommandOp::kMove,2,now+1,100'000'000,0.05F),now+1);
    assert(reply.code==result);
    assert((reply.reserved16 & kReplyNativeMoveRemoteAck)==0U);
    assert(!native_core.armed() && native_core.faulted() && client.stops>0);
    assert(client.moves==1);
    NativeMotionEvidence evidence;
    evidence.MoveAttempt({0.05,0,0},now);
    evidence.MoveReply(false,true,reply.code,now+1);
    assert(evidence.sdk_move_attempt_count==1 && evidence.sdk_move_failure_count==1);
    assert(evidence.sdk_move_dispatch_count==0);
    assert(evidence.last_sdk_move_reply_received);
    assert(!evidence.last_sdk_move_remote_response_verified);
    assert(evidence.last_sdk_move_reply_code==result);

    auto legacy_config=NativeConfig();
    legacy_config.native_postures_only=false;
    FakeClient legacy_client;
    legacy_client.move_result=result;
    DaemonCore legacy_core(legacy_config,legacy_client);
    assert(legacy_core.Handle(
        MakeCommand(CommandOp::kArm,1,now,100'000'000),now).code==0);
    assert(legacy_core.Handle(
        MakeCommand(CommandOp::kMove,2,now+1,100'000'000,0.05F),now+1).code==
        static_cast<std::int32_t>(ReplyCode::kSdkError));
  }
  NativeMotionEvidence timed_out;
  timed_out.MoveAttempt({0.05,0,0},now);
  timed_out.MoveReply(false,false,
                     static_cast<std::int32_t>(ReplyCode::kInternalError),now+1);
  assert(!timed_out.last_sdk_move_reply_received &&
         timed_out.sdk_move_dispatch_count==0 && timed_out.sdk_move_failure_count==1);
}

void NativePosturePreservesSdkErrorsThroughFaultCleanup() {
  constexpr std::uint64_t now=1'000'000'000ULL;
  for (const auto action : {SportAction::kStandDown,SportAction::kStandUp,
                           SportAction::kBalanceStand}) {
    for (const std::int32_t result : {-1,-3104,3104,-4107,7001}) {
      for (const bool native : {true,false}) {
        auto config=NativeConfig();
        config.native_postures_only=native;
        FakeClient client;
        client.sport_action_result=result;
        DaemonCore core(config,client);
        assert(core.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),
                           now).code==0);
        const auto reply=core.Handle(
            MakeSportActionCommand(action,2,now+1,100'000'000),now+1);
        assert(ValidateReply(reply,2));
        assert(reply.code==(native ? result
                                  : static_cast<std::int32_t>(ReplyCode::kSdkError)));
        assert(!core.armed() && core.faulted() && !core.moving());
        assert(reply.armed==0 && reply.faulted==1);
        assert(client.postures==1 && client.last==action);
        assert(client.stops==1 && client.moves==0);
        // Subsequent cleanup has its own success code, without changing the
        // original failed-action reply. No recovery posture is dispatched.
        assert(core.Handle(MakeCommand(CommandOp::kDisarm,3,now+2,100'000'000),
                           now+2).code==0);
        assert(reply.code==(native ? result
                                  : static_cast<std::int32_t>(ReplyCode::kSdkError)));
        assert(client.postures==1 && client.moves==0);
      }
    }
  }
}

void PostureTerminalCompletionDoesNotStopOrLeaveAnActionDeadline() {
  constexpr std::uint64_t now=1'000'000'000ULL;
  for (const auto action : {SportAction::kStandUp,SportAction::kStandDown,
                           SportAction::kBalanceStand}) {
    FakeClient client;
    DaemonCore core(NativeConfig(),client);
    std::uint64_t sequence=1;
    assert(core.Handle(MakeCommand(CommandOp::kArm,sequence++,now,100'000'000),
                       now).code==0);
    assert(core.Handle(MakeSportActionCommand(action,sequence++,now+1,100'000'000),
                       now+1).code==0);
    assert(client.postures==1 && client.last==action);
    client.mode=NativePostureTargetMode(action);
    client.state_received_ns=now+100'000'000ULL;
    const auto completed=now+200'000'000ULL;
    assert(core.Handle(MakeFinishPostureCommand(action,sequence++,completed,100'000'000),
                       completed).code==0);
    assert(core.armed() && !core.moving() && !core.faulted());
    assert(client.stops==0 && client.moves==0 && client.postures==1);
    for (std::uint64_t step=1; step<=100; ++step) {
      const auto idle_now=completed+step*100'000'000ULL;
      assert(core.Handle(MakeCommand(CommandOp::kPing,sequence++,idle_now,100'000'000),
                         idle_now).code==0);
      assert(!core.CheckWatchdog(idle_now));
      assert(client.stops==0); // no delayed 5-second stop after pose completion
    }
    assert(core.armed());
    core.OnDisconnect();
    assert(client.stops>0 && !core.armed() && client.postures==1);
  }
}

void InvalidPostureCompletionStopsAndDisarms() {
  constexpr std::uint64_t now=1'000'000'000ULL;
  auto malformed=MakeFinishPostureCommand(SportAction::kHello,1,now,100'000'000);
  assert(ValidateCommand(malformed,now)==ReplyCode::kMalformed);
  malformed=MakeFinishPostureCommand(SportAction::kStandUp,1,now,100'000'000);
  malformed.vx=0.05;
  Seal(malformed);
  assert(ValidateCommand(malformed,now)==ReplyCode::kMalformed);
  for (int failure=0; failure<5; ++failure) {
    FakeClient client;
    DaemonCore core(NativeConfig(),client);
    assert(core.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now).code==0);
    assert(core.Handle(MakeSportActionCommand(SportAction::kStandUp,2,now+1,100'000'000),
                       now+1).code==0);
    client.mode=6;
    client.state_received_ns=now+100'000'000ULL;
    if (failure==0) client.mode=5; // fresh but incorrect terminal mode
    if (failure==1) client.fresh=false;
    if (failure==2) client.state_received_ns=now; // pre-action state
    const auto action=failure==3 ? SportAction::kStandDown : SportAction::kStandUp;
    const auto completion_ns=failure==4 ? now+5'000'000'001ULL : now+200'000'000ULL;
    const auto reply=core.Handle(
        MakeFinishPostureCommand(action,3,completion_ns,100'000'000),completion_ns);
    assert(reply.code!=0);
    assert(!core.armed() && core.faulted() && client.stops>0);
    assert(client.postures==1); // fault never tries to change posture/mode
  }
  FakeClient timeout_client;
  DaemonCore timeout_core(NativeConfig(),timeout_client);
  timeout_core.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now);
  timeout_core.Handle(
      MakeSportActionCommand(SportAction::kStandUp,2,now+1,100'000'000),now+1);
  const auto expired=now+5'000'000'001ULL;
  timeout_core.Handle(MakeCommand(CommandOp::kPing,3,expired,100'000'000),expired);
  assert(timeout_core.CheckWatchdog(expired));
  assert(timeout_client.stops>0 && !timeout_core.armed());

  auto config=NativeConfig();
  config.native_postures_only=false;
  FakeClient legacy_client;
  DaemonCore legacy_core(config,legacy_client);
  legacy_core.Handle(MakeCommand(CommandOp::kArm,1,now,100'000'000),now);
  assert(legacy_core.Handle(
      MakeFinishPostureCommand(SportAction::kStandUp,2,now+1,100'000'000),now+1).code==
      static_cast<std::int32_t>(ReplyCode::kMotionDisabled));
  assert(legacy_client.moves==0 && legacy_client.postures==0);
}

int main() {
  SignedMotionAndPostures();
  GuardRetainsBothSigns();
  DelayedTicksUseElapsedSlewWithTheExistingStepCeiling();
  IpcPreservesPostureIds();
  PersistentSessionStopsBetweenActions();
  HalfSecondMoveAndExplicitStopHaveNoResidualDispatch();
  NativeMoveRequiresMatchingResponseAndPropagatesRejection();
  NativePosturePreservesSdkErrorsThroughFaultCleanup();
  PostureTerminalCompletionDoesNotStopOrLeaveAnActionDeadline();
  InvalidPostureCompletionStopsAndDisarms();
  std::cout<<"native signed motion/posture/IPC tests: PASS\n";
}
