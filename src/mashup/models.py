from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class Track(BaseModel):
    artist: str
    title: str
    key: str
    bpm: int
    genre: str


class TrackSelection(BaseModel):
    track_a: Track
    track_b: Track
    rationale: str


class TrackBeats(BaseModel):
    audio_file: str
    bpm: float
    beats: list[float]
    downbeats: list[float]
    time_signature: int


class BarFeatures(BaseModel):
    start: float
    end: float
    key: str
    scale: str
    energy: float
    spectral_centroid: float


class Section(BaseModel):
    start: float
    end: float
    label: str
    is_vocal: bool
    mean_energy: float
    mean_spectral_centroid: float


class TrackFeatures(BaseModel):
    audio_file: str
    bpm: float
    time_signature: int
    global_key: str
    global_scale: str
    global_energy: float
    bars: list[BarFeatures]
    sections: list[Section]


# --- Mix planning models ---


class HighPass(BaseModel):
    type: Literal["high_pass"] = "high_pass"
    freq_hz: int


class LowPass(BaseModel):
    type: Literal["low_pass"] = "low_pass"
    freq_hz: int


class Reverb(BaseModel):
    type: Literal["reverb"] = "reverb"
    wet_ratio: float = Field(ge=0.0, le=1.0)


class Delay(BaseModel):
    type: Literal["delay"] = "delay"
    delay_ms: int
    feedback: float = Field(ge=0.0, le=1.0)


class Compressor(BaseModel):
    type: Literal["compressor"] = "compressor"
    threshold_db: float
    ratio: float


TrackEffect = Annotated[
    Union[HighPass, LowPass, Reverb, Delay, Compressor],
    Field(discriminator="type"),
]


class MixTrackRole(BaseModel):
    source_start: float
    source_end: float
    gain_db: float = 0.0
    effects: list[TrackEffect] = []


class MixSlice(BaseModel):
    track_a: MixTrackRole | None = None
    track_b: MixTrackRole | None = None


class MixPlan(BaseModel):
    target_bpm: int
    track_a_pitch_shift_semitones: int
    track_b_pitch_shift_semitones: int
    slices: list[MixSlice]
    rationale: str
