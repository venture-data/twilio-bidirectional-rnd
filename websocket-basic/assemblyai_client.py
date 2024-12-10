import os
import wave
import assemblyai as aai
from uuid import uuid4

class AssemblyAIClient:
    def __init__(self, api_key: str):
        aai.settings.api_key = api_key
        self.transcriber = aai.Transcriber()

    def save_to_wav(self, audio_data: bytes, sample_rate=8000, num_channels=1, sample_width=2) -> str:
        """
        Save PCM audio data to a temporary WAV file.
        Returns the file path.
        """
        temp_filename = f"{uuid4()}.wav"
        with wave.open(temp_filename, 'wb') as wf:
            wf.setnchannels(num_channels)
            wf.setsampwidth(sample_width)     # 2 bytes = 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data)
        return temp_filename

    def transcribe_audio(self, audio_data: bytes) -> str:
        """
        Transcribe the given raw PCM audio data using AssemblyAI.
        """
        wav_path = self.save_to_wav(audio_data)
        try:
            transcript = self.transcriber.transcribe(wav_path)
            return transcript.text
        finally:
            # Cleanup the temporary file
            try:
                os.remove(wav_path)
            except OSError:
                pass
