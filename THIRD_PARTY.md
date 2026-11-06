# Third-party software and redistribution notices

The repository's original integration code is Apache-2.0. The following
pinned upstream components retain their own licenses. This list is an
inventory, not legal advice; distributors remain responsible for satisfying
the applicable terms.

| Component | Use | License | Authoritative text |
|---|---|---|---|
| Unitree SDK2 | Statically linked into the isolated chassis and camera daemons | BSD-3-Clause | `third_party/unitree_sdk2/LICENSE` |
| Unitree ROS 2 | Go2 ROS message definitions | BSD-3-Clause | `third_party/unitree_ros2/LICENSE` |
| Eclipse CycloneDDS | Private `libddsc` runtime shipped beside the SDK daemons | EPL-2.0 OR EDL-1.0 | `third_party/unitree_sdk2/licenses/eclipse-cyclonedds/cyclonedds/LICENSE` |
| Eclipse CycloneDDS-CXX | Private `libddscxx` runtime shipped beside the SDK daemons | EPL-2.0 OR EDL-1.0 | `third_party/unitree_sdk2/licenses/eclipse-cyclonedds/cyclonedds-cxx/LICENSE` |
| Eclipse Iceoryx | Unitree SDK2 dependency | Apache-2.0 | `third_party/unitree_sdk2/licenses/eclipse-iceoryx/iceoryx/LICENSE` |
| RapidJSON and its listed third-party components | Unitree SDK2 dependency | MIT and the notices in the upstream license file | `third_party/unitree_sdk2/licenses/Tencent/rapidjson/LICENSE` |
| Unitree Go2-W URDF/DAE | Native model fetched at build time | BSD-3-Clause | `native/fetch_go2w_model.py` retains upstream `LICENSE.unitree` |
| Optional Unitree Go2 MJCF/OBJ assets | Source-only MuJoCo onboarding download, not vendored | BSD-3-Clause | `unitreerobotics/unitree_mujoco@1eb6642e3f3fdfb7fb13a9794fd6a2dd93ea0e7d/LICENSE`; locked in `sim/onboarding/upstream.lock.json`, copied to generated bundle `vendor/LICENSE` |
| Unitree Go2-W URDF/DAE | Native model fetched at build time, not committed | BSD-3-Clause | `unitreerobotics/unitree_ros` revision in `native/deps.lock`; fetched by `native/fetch_go2w_model.py` |
| `sensor_msgs_py` | Foxy point-cloud helper for the native observer | Apache-2.0 | `ros2/common_interfaces` commit in `native/deps.lock`; installed by `native/fetch-sensor-msgs-py.sh` |
| Audio client bridge | Fetched at build time into `rbnx-boot/cache` | MulanPSL-2.0 | `syswonder/primitive-audio-client-bridge-rbnx` commit in `native/deps.lock` |
| sherpa-onnx SenseVoice, VITS and KWS weights | Speech models downloaded into ignored `rbnx-build/` | Upstream release terms; not this repository's Apache-2.0 grant | `k2-fsa/sherpa-onnx` assets named in `packages/go2w_native_speech/go2w_native_speech/models.py` |
| Mini-JEV and its LoRA | Optional external vision model, not redistributed | Not included | Operator-provided service at `GO2W_JEV_MODEL_URL` |

The root `catalog.license` value `Apache-2.0` covers this repository's
integration code. It does not relicense the rows above. Mapping and navigation
snapshots under `third_party/` keep the licenses in their own trees; the native
launch manifest does not start them.

Both SDK-daemon CMake install rules copy these SDK/runtime license texts into
`share/licenses/unitree_sdk2/` beside the installed image. Do not distribute a
detached daemon binary or private DDS libraries without their corresponding
license directory and this repository's `NOTICE`.
