from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_url: str = Field(min_length=1)
    camera_provider_id: str = "go2w_native"
    chassis_provider_id: str = "go2w_chassis_motion"
    camera_frame_id: str = "front_camera"
    motion_enabled: bool = False
    max_sensor_age_s: float = Field(default=0.5, gt=0.0, le=1.0)
    decision_age_limit_s: float = Field(default=1.5, gt=0.0, le=3.0)
    evaluation_interval_s: float = Field(default=1.0, ge=0.5, le=10.0)
    model_timeout_s: float = Field(default=12.0, gt=0.0, le=30.0)
    capture_timeout_s: float = Field(default=3.0, gt=0.0, le=10.0)
    clear_probability_limit: float = Field(default=0.35, gt=0.0, le=0.5)
    forward_speed_mps: float = Field(default=0.1, gt=0.0, le=0.25)
    forward_duration_s: float = Field(default=0.9, gt=0.0, le=1.0)
    lidar_url: str = "http://127.0.0.1:18092/cloud"
    lidar_timeout_s: float = Field(default=1.0, gt=0.0, le=3.0)
    lidar_frames: int = Field(default=3, ge=1, le=5)
    stop_distance_m: float = Field(default=0.75, ge=0.4, le=3.0)

    @field_validator("model_url")
    @classmethod
    def validate_model_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("model_url must be an HTTP or HTTPS URL")
        return value.rstrip("/")

