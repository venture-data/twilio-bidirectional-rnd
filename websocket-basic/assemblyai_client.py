import os
import wave
import assemblyai as aai
import tempfile

class AssemblyAIClient:
    def __init__(self, api_key: str):
        aai.settings.api_key = api_key
        self.transcriber = aai.Transcriber()

    def transcribe_audio(self, audio_data: bytes) -> str:
        """
        Transcribe the given raw PCM audio data using AssemblyAI.
        """
        # Create a temporary .wav file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = tmp_wav.name
            # Write WAV headers and audio frames
            with wave.open(tmp_wav, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)     # 2 bytes = 16-bit
                wf.setframerate(8000)  # 8kHz
                wf.writeframes(audio_data)

        try:
            transcript = self.transcriber.transcribe(wav_path)
            return transcript.text
        finally:
            # Cleanup the temporary file
            try:
                os.remove(wav_path)
            except OSError:
                pass
