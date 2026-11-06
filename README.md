# Robonix deployment for the Unitree Go2-W

This repository is the robot deployment for a Unitree Go2-W running Ubuntu
20.04, ROS 2 Foxy, and the onboard Orin NX. Its catalog name is
`robonix.robot.unitree.go2w`.

The launch manifest is the repository-root `robonix_manifest.yaml`.
`native/robonix_manifest.yaml` points at that same file.

```bash
bash build.sh
bash native/validate.sh
bash start.sh
```

`build.sh` and `start.sh` use the native profile. It does not start a
container and does not change the host network. Motion stays disabled until
an operator enables it explicitly. See [native/README.md](native/README.md)
for sensor topics, process layout, and service commands.
