from asyncio import Semaphore
import base64
import io
import os
import tempfile
from threading import Lock
from typing import List, Literal
import wave

import numpy as np
import torch
from fastapi import (
    FastAPI,
    UploadFile,
    Body,
    HTTPException,
)
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import Xtts
from TTS.utils.generic_utils import get_user_data_dir
from TTS.utils.manage import ModelManager

torch.set_num_threads(2)
device = torch.device("cuda")

model_path = '/home/ubuntu/XTTS-v2/'

# Create a lock
lock = Lock()


print("Loading XTTS",flush=True)
config = XttsConfig()
config.load_json(os.path.join(model_path, "config.json"))
model = Xtts.init_from_config(config)
model.load_checkpoint(config, checkpoint_dir=model_path, eval=True, use_deepspeed=True)
model.to(device)
print("XTTS Loaded.",flush=True)

print("Running XTTS Server ...",flush=True)

##### Run fastapi #####
app = FastAPI(
    title="XTTS Streaming server",
    description="""XTTS Streaming server""",
    version="0.0.1",
    docs_url="/jhdc",
)


@app.post("/clone_speaker")
def predict_speaker(wav_file: UploadFile):
    """Compute conditioning inputs from reference audio file."""
    # temp_audio_name = next(tempfile._get_candidate_names())
    # with open(temp_audio_name, "wb") as temp, torch.inference_mode():
    #     temp.write(io.BytesIO(wav_file.file.read()).getbuffer())
    #     gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
    #         temp_audio_name
    #     )
    lock.acquire()
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            temp.write(io.BytesIO(wav_file.file.read()).getbuffer())
            temp.flush()  # Ensure all data is written to the file
            temp_path = temp.name

        gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(temp_path)

        # Clean up the temporary file
        os.remove(temp_path)

        result = {
            "gpt_cond_latent": gpt_cond_latent.cpu().squeeze().half().tolist(),
            "speaker_embedding": speaker_embedding.cpu().squeeze().half().tolist(),
        }
        return result
    finally:
        lock.release()


def postprocess(wav):
    """Post process the output waveform"""
    if isinstance(wav, list):
        wav = torch.cat(wav, dim=0)
    wav = wav.clone().detach().cpu().numpy()
    wav = wav[None, : int(wav.shape[0])]
    wav = np.clip(wav, -1, 1)
    wav = (wav * 32767).astype(np.int16)
    return wav


def encode_audio_common(
    frame_input, encode_base64=True, sample_rate=24000, sample_width=2, channels=1
):
    """Return base64 encoded audio"""
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as vfout:
        vfout.setnchannels(channels)
        vfout.setsampwidth(sample_width)
        vfout.setframerate(sample_rate)
        vfout.writeframes(frame_input)

    wav_buf.seek(0)
    if encode_base64:
        b64_encoded = base64.b64encode(wav_buf.getbuffer()).decode("utf-8")
        return b64_encoded
    else:
        return wav_buf.read()


class StreamingInputs(BaseModel):
    speaker_embedding: List[float]
    gpt_cond_latent: List[List[float]]
    text: str
    language: Literal[
        "en",
        "de",
        "fr",
        "es",
        "it",
        "pl",
        "pt",
        "tr",
        "ru",
        "nl",
        "cs",
        "ar",
        "zh",
        "ja",
        "hu",
        "ko",
    ]
    add_wav_header: bool = True
    stream_chunk_size: str = "20"


def predict_streaming_generator(parsed_input: dict = Body(...)):
    speaker_embedding = (
        torch.tensor(parsed_input.speaker_embedding).unsqueeze(0).unsqueeze(-1)
    )
    gpt_cond_latent = (
        torch.tensor(parsed_input.gpt_cond_latent).reshape((-1, 1024)).unsqueeze(0)
    )
    text = parsed_input.text
    language = parsed_input.language

    stream_chunk_size = int(parsed_input.stream_chunk_size)
    add_wav_header = parsed_input.add_wav_header


    chunks = model.inference_stream(
        text,
        language,
        gpt_cond_latent,
        speaker_embedding,
        stream_chunk_size=stream_chunk_size,
        enable_text_splitting=True,
        speed=1.2
    )

    for i, chunk in enumerate(chunks):
        chunk = postprocess(chunk)
        if i == 0 and add_wav_header:
            yield encode_audio_common(b"", encode_base64=False)
            yield chunk.tobytes()
        else:
            yield chunk.tobytes()

def streaming_wrapper(lock, streaming_generator):
    try:
        # Yield from the original streaming generator
        for item in streaming_generator:
            yield item
    finally:
        # Release the semaphore when streaming is done
        lock.release()

# @app.post("/tts_stream")
# def predict_streaming_endpoint(parsed_input: StreamingInputs):
#     return StreamingResponse(
#         predict_streaming_generator(parsed_input),
#         media_type="audio/wav",
#     )
@app.post("/tts_stream")
def predict_streaming_endpoint(parsed_input: StreamingInputs):
    # Acquire the semaphore
    lock.acquire()
    # Wrap the original generator
    wrapped_generator = streaming_wrapper(lock, predict_streaming_generator(parsed_input))

    # Create a StreamingResponse with the wrapped generator
    return StreamingResponse(wrapped_generator, media_type="audio/wav")