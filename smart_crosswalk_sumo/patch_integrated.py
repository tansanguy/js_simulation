import re
from pathlib import Path

content = Path('integrated_mode.py').read_text()

# Add attempt_route_repair to imports
content = content.replace("from .network_utils import (", "from .network_utils import (\n    attempt_route_repair,")
content = content.replace("from network_utils import (", "from network_utils import (\n    attempt_route_repair,")

Path('integrated_mode.py').write_text(content)
