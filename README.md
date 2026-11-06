# Robonix deployment for the Unitree Go2-W

This repository is the robot deployment for a Unitree Go2-W running Ubuntu
20.04, ROS 2 Foxy, and the onboard Orin NX. Its catalog name is
`robonix.robot.unitree.go2w`.

The launch manifest is the repository-root `robonix_manifest.yaml`.
`native/robonix_manifest.yaml` points at that same file.

```bash
python3 native/fetch_go2w_model.py
bash build.sh
bash native/build-motion.sh
bash native/validate.sh
bash start.sh
```

`build.sh` also fetches the pinned audio bridge and ROS Foxy `sensor_msgs_py`.
The Go2-W model download above has to happen first; the build stops if that
model is missing. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

`native/build-motion.sh` separately compiles the SDK daemon and guarded ROS
adapter required by the chassis `enable` primitive. It only builds artifacts
and runs offline tests; it does not enable the chassis or execute a posture.

`build.sh` and `start.sh` use the native profile. It does not start a
container and does not change the host network. Motion stays disabled until
an operator enables it explicitly. See [native/README.md](native/README.md)
for sensor topics, process layout, and service commands.
