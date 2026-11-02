from pathlib import Path
import hashlib
ROOT=Path(__file__).resolve().parent
def summary():
    return {'version':'3.1-skill.1','launcher_revision':'2026-10-31-empty','mode':'empty-skill',
            'instance_id':hashlib.sha256(ROOT.resolve().as_posix().lower().encode('utf-8')).hexdigest()}
