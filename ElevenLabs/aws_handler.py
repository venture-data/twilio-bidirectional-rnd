import boto3
import os
from dotenv import load_dotenv

load_dotenv()

class AWSHandler:
    def __init__(self):
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
        )
        self.bucket_name = 'callspro-dev-private'

    def upload_recording(self, local_path: str, call_sid: str, recording_sid: str) -> str:
        """
        Uploads a recording to S3 in calls-recordings/{recording_sid}.mp3 format
        
        Args:
            local_path: Path to local MP3 file
            call_sid: Twilio Call SID for directory structure
            recording_sid: Recording SID for filename
            
        Returns:
            S3 object URL
        """
        try:
            s3_key = f'calls-recordings/{call_sid}/{recording_sid}.mp3'
            self.s3_client.upload_file(
                Filename=local_path,
                Bucket=self.bucket_name,
                Key=s3_key
            )
            print(f"📤 Uploaded {local_path} to S3: s3://{self.bucket_name}/{s3_key}")
            return f"s3://{self.bucket_name}/{s3_key}"
        except Exception as e:
            print(f"🔥 Failed to upload recording to S3: {e}")
            raise e