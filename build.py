import subprocess
import sys
from pathlib import Path

def run_step(cmd: list[str], description: str):
    print(f"\n=========================================")
    print(f"Step: {description}")
    print(f"Running: {' '.join(cmd)}")
    print(f"=========================================")
    try:
        result = subprocess.run(cmd, check=True, shell=sys.platform == "win32")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] Step failed: {description}")
        print(f"Command returned non-zero exit code: {e.returncode}")
        sys.exit(e.returncode)
    except FileNotFoundError:
        print(
            f"\n[ERROR] Command not found. Make sure {' '.join(cmd)} is available in path."
        )
        sys.exit(1)

def clean_previous_builds():
    import shutil
    project_dir = Path(__file__).resolve().parent
    dist_dir = project_dir / "dist"
    if dist_dir.exists() and dist_dir.is_dir():
        print(f"Cleaning previous build directory: {dist_dir}")
        try:
            shutil.rmtree(dist_dir)
        except Exception as e:
            print(f"Warning: Failed to clean {dist_dir}: {e}")

def main():
    clean_previous_builds()
    
    # 1. Sync project environment
    run_step(["uv", "sync"], "Synchronizing environment & dependencies")

    # 2. Run unit tests
    run_step(["uv", "run", "pytest"], "Running pytest suite")

    # 3. Build packaging artifacts
    run_step(["uv", "build"], "Building source and wheel packages")

    # 4. Copy artifacts to ARTIFACTS_ROOT if set
    copy_artifacts()

    print("\n[SUCCESS] Build pipeline completed successfully!")

def get_project_metadata():
    import re
    project_dir = Path(__file__).resolve().parent
    pyproject_path = project_dir / "pyproject.toml"
    content = pyproject_path.read_text(encoding="utf-8")
    
    name_match = re.search(r'name\s*=\s*"([^"]+)"', content)
    version_match = re.search(r'version\s*=\s*"([^"]+)"', content)
    
    name = name_match.group(1) if name_match else "unknown"
    version = version_match.group(1) if version_match else "0.1.0"
    return name, version

def copy_artifacts():
    import shutil
    import os
    artifacts_root = os.environ.get("ARTIFACTS_ROOT")
    if not artifacts_root:
        print("\n[INFO] ARTIFACTS_ROOT environment variable not set. Skipping artifact copy.")
        return
        
    app_name, version = get_project_metadata()
    target_dir = Path(artifacts_root) / app_name / version
    
    project_dir = Path(__file__).resolve().parent
    dist_dir = project_dir / "dist"
    if dist_dir.exists() and dist_dir.is_dir():
        dest_dist = target_dir / "dist"
        dest_dist.mkdir(parents=True, exist_ok=True)
        print(f"Copying build artifacts from {dist_dir} to {dest_dist}...")
        for item in dist_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, dest_dist / item.name)
            elif item.is_dir():
                shutil.copytree(item, dest_dist / item.name, dirs_exist_ok=True)

if __name__ == "__main__":
    main()
