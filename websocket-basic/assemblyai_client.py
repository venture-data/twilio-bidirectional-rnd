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

        # Buffer for audio chunks we receive
        self.audio_chunks = []
        self.streaming_started = False

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

    # ------------------ Realtime Transcription Methods ------------------ #

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        self.session_opened = True
        print("AssemblyAI Realtime: Session ID:", session_opened.session_id)

    def on_data(self, transcript: aai.RealtimeTranscript):
        print("on_data called with transcript:", transcript)
        if not transcript.text:
            print("No text in transcript. Possibly silence or unrecognized audio.")
            return
        
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            self.final_transcripts.append(transcript.text)
            print("Final:", transcript.text)
        else:
            self.partial_transcript = transcript.text
            print("Partial:", transcript.text)

    def on_error(self, error: aai.RealtimeError):
        print("AssemblyAI Realtime Error:", error)

    def on_close(self):
        self.session_ended = True
        print("AssemblyAI Realtime: Closing Session")

    def start_realtime_transcription_session(self, sample_rate=8000):
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
        self.audio_chunks = []
        self.streaming_started = False

        self.realtime_transcriber = aai.RealtimeTranscriber(
            sample_rate=8000,
            on_data=self.on_data,
            on_error=self.on_error,
            on_open=self.on_open,
            on_close=self.on_close,
            encoding=aai.AudioEncoding.pcm_mulaw,
            disable_partial_transcripts=False  # Set to True if you want only final transcripts
        )
        self.realtime_transcriber.connect()

    def add_audio_chunk(self, audio_data: bytes):
        """
        Add a chunk of linear PCM 16-bit mono at 8kHz to the buffer.
        We will stream these chunks when we start_streaming_audio().
        """
        if not self.realtime_transcriber or self.session_ended:
            return
        self.audio_chunks.append(audio_data)

    def audio_generator(self):
        for i, chunk in enumerate(self.audio_chunks):
            print(f"Audio generator yielding chunk {i}, size={len(chunk)} bytes")
            yield chunk

    def start_streaming_audio(self):
        """
        Start streaming the audio from self.audio_chunks to AssemblyAI
        using the RealtimeTranscriber.stream() method.
        This should be called once you've started adding chunks.
        """
        if not self.realtime_transcriber or self.session_ended:
            print("Cannot start streaming: no active realtime session.")
            return

        if self.streaming_started:
            print("Already streaming audio.")
            return

        self.streaming_started = True
        # Start streaming and block until we run out of chunks
        self.realtime_transcriber.stream(self.audio_generator())

    def stop_realtime_transcription(self):
        """
        Stop sending audio and close the realtime transcriber session.
        Returns the final transcripts as a single string.
        """
        if self.realtime_transcriber and not self.session_ended:
            self.realtime_transcriber.close()

        # Return final transcripts as a single string
        return "\n".join(self.final_transcripts)