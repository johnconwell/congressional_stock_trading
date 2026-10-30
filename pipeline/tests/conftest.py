import sys
from pathlib import Path

# Let tests `import config` the same way build.py does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
