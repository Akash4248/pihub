import subprocess
import os
import sys

pdf_path = "/home/akash/Desktop/NOTIMP/TEXTBOOKS/science/class 10/electricity.pdf"
output_dir = "/home/akash/Desktop/PIHUB/generated_pack_new/electricity"

cmd = [
    "/home/akash/Desktop/IDP DOCS/venv/bin/python3", "-m", "skill_anything.cli", "pdf", pdf_path,
    "--format", "all", "--output", output_dir
]

env = os.environ.copy()
env["PYTHONPATH"] = "/home/akash/Desktop/IDP DOCS/Skill-Anything"

print(f"Running Skill-Anything on {pdf_path}...")
subprocess.run(cmd, env=env, check=True)
print(f"Generation complete! Check {output_dir}")
