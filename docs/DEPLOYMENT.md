# Deployment

This repository deploys a Unitree Go2-W with the native Foxy profile.

```bash
git clone --recursive git@github.com:LittleRookie1115/robot-unitree-go2w.git
cd robot-unitree-go2w
bash build.sh
bash start.sh
```

The root `robonix_manifest.yaml` is the launch manifest. Sensor topics,
speech, and motion limits are documented in [native/README.md](../native/README.md).
Keep motion disabled until the operator enables it for a supervised test.
