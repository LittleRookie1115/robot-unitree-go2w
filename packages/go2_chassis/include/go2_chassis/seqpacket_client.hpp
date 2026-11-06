#pragma once

#include <cerrno>
#include <chrono>
#include <cstring>
#include <string>
#include <utility>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include "go2_chassis/motion_timing.hpp"
#include "go2_chassis/protocol.hpp"
#include "go2_chassis/safety_guard.hpp"

namespace go2_chassis {

class SeqpacketClient {
 public:
  explicit SeqpacketClient(std::string socket_path)
      : socket_path_(std::move(socket_path)) {}

  ~SeqpacketClient() { Close(); }

  bool connected() const { return descriptor_ >= 0; }
  bool last_reply_received() const { return last_reply_received_; }
  std::uint16_t last_reply_flags() const { return last_reply_flags_; }

  bool Connect(std::string *error) {
    if (connected()) {
      return true;
    }
    if (socket_path_.empty() || socket_path_.size() >= sizeof(sockaddr_un::sun_path)) {
      SetError(error, "IPC socket path is empty or too long");
      return false;
    }
    descriptor_ = ::socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (descriptor_ < 0) {
      SetError(error, "cannot create IPC socket");
      return false;
    }
    timeval timeout {};
    timeout.tv_sec = kMotionArmIpcReplyTimeoutMs / 1000;
    timeout.tv_usec = (kMotionArmIpcReplyTimeoutMs % 1000) * 1000;
    (void)::setsockopt(descriptor_, SOL_SOCKET, SO_SNDTIMEO, &timeout,
                       sizeof(timeout));
    (void)::setsockopt(descriptor_, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                       sizeof(timeout));

    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::strncpy(address.sun_path, socket_path_.c_str(),
                 sizeof(address.sun_path) - 1U);
    if (::connect(descriptor_, reinterpret_cast<const sockaddr *>(&address),
                  sizeof(address)) != 0) {
      SetError(error, "cannot connect to SDK daemon IPC socket");
      Close();
      return false;
    }

    ucred credentials {};
    socklen_t length = sizeof(credentials);
    if (::getsockopt(descriptor_, SOL_SOCKET, SO_PEERCRED, &credentials,
                     &length) != 0 ||
        length != sizeof(credentials) || credentials.uid != ::geteuid()) {
      SetError(error, "SDK daemon IPC peer UID does not match");
      Close();
      return false;
    }
    return true;
  }

  bool Exchange(CommandOp operation, const Velocity &velocity,
                std::int32_t *reply_code, std::string *error,
                SportAction action = SportAction::kHello) {
    last_reply_received_ = false;
    last_reply_flags_ = 0U;
    if (reply_code != nullptr) {
      *reply_code = static_cast<std::int32_t>(ReplyCode::kInternalError);
    }
    if (!connected() && !Connect(error)) {
      return false;
    }
    const auto now = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());
    const auto sequence = ++sequence_;
    const CommandPacket command =
        operation == CommandOp::kSportAction
            ? MakeSportActionCommand(action, sequence, now, 100'000'000ULL)
            : (operation == CommandOp::kFinishPosture
                   ? MakeFinishPostureCommand(action, sequence, now, 100'000'000ULL)
                   : MakeCommand(operation, sequence, now, 100'000'000ULL,
                                 static_cast<float>(velocity.vx),
                                 static_cast<float>(velocity.vy),
                                 static_cast<float>(velocity.wz)));
    const ssize_t sent =
        ::send(descriptor_, &command, sizeof(command), MSG_NOSIGNAL);
    if (sent != static_cast<ssize_t>(sizeof(command))) {
      SetError(error, "incomplete IPC command write");
      Close();
      return false;
    }

    pollfd poll_descriptor {descriptor_, POLLIN, 0};
    const int reply_timeout_ms = MotionIpcReplyTimeoutMs(operation);
    const int poll_result =
        ::poll(&poll_descriptor, 1, reply_timeout_ms);
    if (poll_result != 1 || (poll_descriptor.revents & POLLIN) == 0) {
      SetError(error, "SDK daemon IPC reply timeout");
      Close();
      return false;
    }
    ReplyPacket reply {};
    const ssize_t received = ::recv(descriptor_, &reply, sizeof(reply), 0);
    if (received != static_cast<ssize_t>(sizeof(reply)) ||
        !ValidateReply(reply, command.sequence)) {
      SetError(error, "invalid SDK daemon IPC reply");
      Close();
      return false;
    }
    last_reply_received_ = true;
    last_reply_flags_ = reply.reserved16;
    if (reply_code != nullptr) {
      *reply_code = reply.code;
    }
    if (reply.code != static_cast<std::int32_t>(ReplyCode::kOk)) {
      SetError(error, "SDK daemon rejected command with code " +
                          std::to_string(reply.code));
      return false;
    }
    return true;
  }

  void Close() {
    if (descriptor_ >= 0) {
      ::close(descriptor_);
      descriptor_ = -1;
    }
  }

 private:
  static void SetError(std::string *target, const std::string &message) {
    if (target != nullptr) {
      *target = message;
    }
  }

  std::string socket_path_;
  int descriptor_{-1};
  std::uint64_t sequence_{0};
  bool last_reply_received_{false};
  std::uint16_t last_reply_flags_{0U};
};

}  // namespace go2_chassis
