from pathlib import Path
s = Path('src/bucket.lua').read_text()
Path('build/bucket_script.hpp').write_text('#pragma once\ninline constexpr const char* BUCKET_SCRIPT=R"LUA(' + s + ')LUA";\n')
