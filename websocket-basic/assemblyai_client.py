import os
import wave
import assemblyai as aai
import tempfile
from threading import Event

class AssemblyAIClient:
    def __init__(self, api_key: str):
        aai.settings.api_key = api_key
        self.transcriber = aai.Transcriber()

        # Realtime transcription attributes
        self.realtime_transcriber = None
        self.final_transcripts = []
        self.partial_transcript = ""
        self.session_opened = False
        self.session_ended = False
        self.streaming_started = False
        self.session_open_event = Event()

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
        self.session_open_event.set()

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

    def start_realtime_transcription_session(self):
        """
        Start a realtime transcription session with 16kHz, PCM S16LE encoding.
        We'll store partial and final transcripts and finalize after stopping.
        """
        # Reset state
        self.final_transcripts = []
        self.partial_transcript = ""
        self.session_opened = False
        self.session_ended = False
        self.streaming_started = False
        self.session_open_event.clear()

        self.realtime_transcriber = aai.RealtimeTranscriber(
            sample_rate=16000,
            on_data=self.on_data,
            on_error=self.on_error,
            on_open=self.on_open,
            on_close=self.on_close,
            encoding=aai.AudioEncoding.pcm_s16le,
            disable_partial_transcripts=False
        )
        print("Connecting to AssemblyAI...")
        try:
            self.realtime_transcriber.connect()
        except Exception as e:
            print(f"Error connecting to AssemblyAI: {e}")
            return
        print("Connect call finished.")

    def send_audio_chunk(self, audio_data: bytes):
        """
        Immediately send a chunk of linear PCM (16kHz, 16-bit mono) audio
        to AssemblyAI's realtime transcriber.
        """
        # Ensure session exists and is not ended
        if not self.realtime_transcriber or self.session_ended:
            return

        # Optionally wait for session to open
        if not self.session_opened:
            # If you want to queue chunks until open, you'd handle that here
            print("Session not open yet; dropping or queueing chunk.")
            return

        # Send the chunk directly to the transcriber
        self.realtime_transcriber.send(audio_data)

    def stop_realtime_transcription(self):
        """
        Stop sending audio and close the realtime transcriber session.
        Returns the final transcripts as a single string.
        """
        if self.realtime_transcriber and not self.session_ended:
            self.realtime_transcriber.close()

        # Return final transcripts as a single string
        return "\n".join(self.final_transcripts)
