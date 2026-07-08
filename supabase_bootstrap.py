#!/usr/bin/env python3
"""Root launcher for deploy/supabase_bootstrap.py.

Allows running:
  python supabase_bootstrap.py bootstrap --write-env .env
from repository root.
"""

from deploy.supabase_bootstrap import main


if __name__ == "__main__":
    main()
