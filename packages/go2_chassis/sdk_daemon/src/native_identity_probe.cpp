// A read-only RPC query. Never constructs SportClient or selects/releases mode.
#include <iostream>
#include <string>
#include <unitree/robot/channel/channel_factory.hpp>
#include <unitree/robot/b2/motion_switcher/motion_switcher_client.hpp>
#include <unitree/common/json/jsonize.hpp>

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  try {
    unitree::robot::ChannelFactory::Instance()->Init(0, argv[1]);
    unitree::robot::b2::MotionSwitcherClient client;
    client.SetTimeout(1.0F);
    client.Init();
    std::string form, name;
    const auto code = client.CheckMode(form, name);
    unitree::common::JsonMap result;
    result["code"] = code;
    result["form"] = form;
    result["name"] = name;
    result["read_only"] = true;
    std::cout << unitree::common::ToJsonString(result) << std::endl;
    return code == 0 ? 0 : 1;
  } catch (const std::exception &error) {
    std::cerr << error.what() << std::endl;
    return 2;
  }
}
