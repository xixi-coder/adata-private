import os
import tarfile


def create_tar_gz(archive_path: str, paths: list[str], root_dir: str):
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    tmp_path = f"{archive_path}.tmp"
    with tarfile.open(tmp_path, "w:gz") as tar:
        for path in paths:
            full_path = os.path.join(root_dir, path)
            if not os.path.exists(full_path):
                continue
            tar.add(full_path, arcname=path)
    os.replace(tmp_path, archive_path)


def extract_tar_gz(archive_path: str, root_dir: str):
    if not os.path.exists(archive_path):
        raise FileNotFoundError(archive_path)
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(path=root_dir)
