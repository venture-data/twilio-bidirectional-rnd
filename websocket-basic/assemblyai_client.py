import os
import wave
import assemblyai as aai
import tempfile
import audioop
from uuid import uuid4
from typing import List

class AssemblyAIClient:
    def __init__(self, api_key: str):
        aai.settings.api_key = api_key
        self.transcriber = aai.Transcriber()

        # Attributes for realtime streaming
        self.realtime_transcriber = None
        self.final_transcripts = []
        self.partial_transcript = ""
        self.session_opened = False
        self.session_ended = False

    def transcribe_audio(self, audio_data: bytes) -> str:
        """
        Offline transcription of raw PCM audio data (8kHz, 16-bit mono).
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = tmp_wav.name
            with wave.open(tmp_wav, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)     # 16-bit
                wf.setframerate(8000)  # 8kHz
                wf.writeframes(audio_data)

        try:
            transcript = self.transcriber.transcribe(wav_path)
            return transcript.text
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass

    # ------------------ New Realtime Transcription Methods ------------------ #

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        self.session_opened = True
        print("AssemblyAI Realtime: Session ID:", session_opened.session_id)

    def on_data(self, transcript: aai.RealtimeTranscript):
        # Handle partial and final transcripts
        if not transcript.text:
            return
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            # Final transcript, store and newline
            self.final_transcripts.append(transcript.text)
            print("Final:", transcript.text)
        else:
            # Partial transcript (interim)
            self.partial_transcript = transcript.text
            print("Partial:", transcript.text, end="\r")

    def on_error(self, error: aai.RealtimeError):
        print("AssemblyAI Realtime Error:", error)

    def on_close(self):
        self.session_ended = True
        print("AssemblyAI Realtime: Closing Session")

    def start_realtime_transcription_session(self, sample_rate=16000):
        """
        Start a realtime transcription session.
        We'll store partial and final transcripts and later
        finalize the transcript after stopping.
        """
        # Reset state
        self.final_transcripts = []
        self.partial_transcript = ""
        self.session_opened = False
        self.session_ended = False

        self.realtime_transcriber = aai.RealtimeTranscriber(
            sample_rate=sample_rate,
            on_data=self.on_data,
            on_error=self.on_error,
            on_open=self.on_open,
            on_close=self.on_close,
        )
        self.realtime_transcriber.connect()

    def send_audio_chunk(self, audio_data: bytes, input_sample_rate=8000):
        """
        Send a chunk of audio (linear PCM 16-bit mono at 8kHz) to the realtime transcriber.
        We need to resample it to 16kHz before sending.
        """

        if not self.realtime_transcriber or self.session_ended:
            # If session not started or already ended, ignore
            return

        # Resample from 8kHz to 16kHz using audioop
        # audioop.ratecv(data, width, channels, in_rate, out_rate, state)
        # width=2 for 16-bit samples, channels=1, in_rate=8000, out_rate=16000
        state = None
        out_data, state = audioop.ratecv(audio_data, 2, 1, input_sample_rate, 16000, state)

        # Now send the upsampled audio to AssemblyAI
        self.realtime_transcriber.send(out_data)

    def stop_realtime_transcription(self):
        """
        Stop sending audio and close the realtime transcriber session.
        Wait for the session to close and return the final transcripts.
        """
        if self.realtime_transcriber:
            self.realtime_transcriber.close()

        # Wait until on_close is called (or a short timeout)
        # This code assumes synchronous operation. If needed, we can do async sleeps or checks.
        # For simplicity, let's just trust that close is synchronous here. If async needed,
        # we could implement a polling or waiting mechanism.

        # Return final transcripts as a single string
        return "\n".join(self.final_transcripts)
