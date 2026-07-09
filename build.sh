#!/usr/bin/env bash
# Copyright (C) 2026 Clover Ross
# Copyright (C) 2026 Daniel Miller
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Modified in 2026 by Daniel Miller, whose fork this is. What changed and
# when is recorded in AUTHORS and CHANGELOG.md (GPL-3.0 section 5(a)).
#
# bridget is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. bridget is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with
# bridget. If not, see <https://www.gnu.org/licenses/>.

# bridget has no compile step. Validate Python syntax.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3 -m py_compile bridget
echo "build.sh: syntax ok"
