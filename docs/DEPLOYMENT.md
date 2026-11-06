# Deployment

This repository deploys a Unitree Go2-W with the native Foxy profile.

```bash
git clone git@github.com:LittleRookie1115/robot-unitree-go2w.git
cd robot-unitree-go2w
python3 native/fetch_go2w_model.py
bash build.sh
bash native/build-motion.sh
bash start.sh
```

Third-party SDK and message snapshots are already in `third_party/` and are
checked by `scripts/verify_submodule_pins.sh`. Do not expect `git submodule
update` to fetch them. The native build clones the audio bridge commit named
in `native/deps.lock` into `rbnx-boot/cache/` and installs pinned
`sensor_msgs_py` for `/usr/bin/python3`.

Run `native/build-motion.sh` after the base build to prepare the SDK daemon
and guarded ROS adapter used by chassis `enable`. These artifacts belong to
this checkout and are not included in a Git clone or recreated by codegen.
The motion build does not start either backend or grant motion permission.

This host also needs `/opt/ros/foxy` and a CycloneDDS overlay. The default
overlay is `/home/unitree/cyclonedds_ws/install/setup.bash`; set
`CYCLONEDDS_SETUP` when it lives somewhere else. Mini-JEV weights are not in
this repository. The visual service uses `GO2W_JEV_MODEL_URL` only when that
external model is already running. Laptop voice setup is
[docs/laptop-voice.md](laptop-voice.md).

The root `robonix_manifest.yaml` is the launch manifest. Sensor topics,
speech, and motion limits are documented in [native/README.md](../native/README.md).
Keep motion disabled until the operator enables it for a supervised test.
