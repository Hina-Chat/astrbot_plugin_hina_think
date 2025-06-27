import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError
from pathlib import Path
import logging

class R2UploadError(Exception):
    """Custom exception for R2 upload failures."""
    pass

def upload_file_to_r2(local_path: Path, object_key: str, r2_account_id: str, r2_access_key_id: str, r2_secret_access_key: str, r2_bucket_name: str, r2_custom_domain: str = "") -> str:
    """
    上傳本地檔案到 R2，返回 public link
    """
    endpoint_url = f'https://{r2_account_id}.r2.cloudflarestorage.com'

    # 配置 boto3 客戶端，設置網絡超時
    boto_config = Config(
        connect_timeout=10,  # 10秒連接超時
        read_timeout=10,     # 10秒讀取超時
        retries={'max_attempts': 2},
        signature_version='s3v4' # 保持原有的簽名版本配置
    )

    s3_client = boto3.client(
        's3',
        endpoint_url=endpoint_url,
        aws_access_key_id=r2_access_key_id,
        aws_secret_access_key=r2_secret_access_key,
        config=boto_config
    )
    try:
        s3_client.upload_file(str(local_path), r2_bucket_name, object_key)
    except (ClientError, BotoCoreError) as e:
        logging.error(f"R2 Upload Failed for object {object_key}: {e}")
        raise R2UploadError(f"Failed to upload to R2: {e}") from e

    if r2_custom_domain:
        return f"https://{r2_custom_domain}/{object_key}"
    else:
        return f"{endpoint_url}/{r2_bucket_name}/{object_key}"
