#!/usr/bin/env bash
# Blast Radius Engine — первоначальная установка зависимостей
# Использование: ./setup.sh [путь-к-репо]
#
# Скрипт:
# 1. Определяет языки в репозитории
# 2. Устанавливает системные инструменты (ctags, rg, jq)
# 3. Устанавливает Python-зависимости
# 4. Клонирует грамматики tree-sitter для найденных языков
# 5. Устанавливает опциональные языко-специфичные инструменты

set -euo pipefail

REPO_PATH="${1:-$(pwd)}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Цвета
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[setup]${NC} $*"; }
ok()   { echo -e "${GREEN}[ok]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
fail() { echo -e "${RED}[fail]${NC} $*" >&2; }

# ─────────────────────────────────────────────────────────────────────────────
# 1. Детекция языков и конфигов в репозитории
# ─────────────────────────────────────────────────────────────────────────────

detect_languages() {
    local repo="$1"
    log "Сканирую языки в: $repo"

    declare -A found=()

    # Java
    if find "$repo" -name "*.java" -not -path "*/target/*" -not -path "*/.git/*" -quit 2>/dev/null | grep -q .; then
        found[java]=1
    fi
    # Python
    if find "$repo" -name "*.py" -not -path "*/__pycache__/*" -quit 2>/dev/null | grep -q .; then
        found[python]=1
    fi
    # Go
    if find "$repo" -name "*.go" -quit 2>/dev/null | grep -q .; then
        found[go]=1
    fi
    # JavaScript / TypeScript
    if find "$repo" \( -name "*.js" -o -name "*.ts" -o -name "*.jsx" -o -name "*.tsx" \) \
        -not -path "*/node_modules/*" -quit 2>/dev/null | grep -q .; then
        found[javascript]=1
    fi
    # Rust
    if find "$repo" -name "*.rs" -quit 2>/dev/null | grep -q .; then
        found[rust]=1
    fi
    # C/C++
    if find "$repo" \( -name "*.c" -o -name "*.cpp" -o -name "*.cc" -o -name "*.cxx" \) \
        -not -path "*/build/*" -quit 2>/dev/null | grep -q .; then
        found[c_cpp]=1
    fi
    # PHP
    if find "$repo" -name "*.php" -quit 2>/dev/null | grep -q .; then
        found[php]=1
    fi
    # Ruby
    if find "$repo" \( -name "*.rb" -o -name "Gemfile" \) -quit 2>/dev/null | grep -q .; then
        found[ruby]=1
    fi
    # Kotlin
    if find "$repo" -name "*.kt" -quit 2>/dev/null | grep -q .; then
        found[kotlin]=1
    fi
    # Scala
    if find "$repo" -name "*.scala" -quit 2>/dev/null | grep -q .; then
        found[scala]=1
    fi
    # Конфиги
    if find "$repo" -name "nginx.conf" -o -name "*.nginx" 2>/dev/null | grep -q .; then
        found[nginx]=1
    fi
    if find "$repo" \( -name "docker-compose.yml" -o -name "docker-compose.yaml" \) \
        2>/dev/null | grep -q .; then
        found[docker]=1
    fi

    # Вывод найденных языков через пробел
    echo "${!found[@]}"
}

DETECTED_LANGS=$(detect_languages "$REPO_PATH")
log "Обнаружены: ${DETECTED_LANGS:-<ничего>}"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Системные инструменты (universal-ctags, ripgrep, jq)
# ─────────────────────────────────────────────────────────────────────────────

install_system_tools() {
    log "Проверяю системные инструменты..."

    if ! command -v ctags &>/dev/null || ! ctags --version 2>&1 | grep -qi "universal"; then
        warn "universal-ctags не найден"
        if command -v brew &>/dev/null; then
            log "  brew install universal-ctags"
            brew install universal-ctags
        elif command -v apt-get &>/dev/null; then
            log "  apt-get install universal-ctags"
            sudo apt-get install -y universal-ctags
        else
            warn "  Установите universal-ctags вручную: https://ctags.io"
        fi
    else
        ok "universal-ctags: $(ctags --version | head -1)"
    fi

    if ! command -v rg &>/dev/null; then
        warn "ripgrep не найден"
        if command -v brew &>/dev/null; then
            brew install ripgrep
        elif command -v apt-get &>/dev/null; then
            sudo apt-get install -y ripgrep
        fi
    else
        ok "ripgrep: $(rg --version | head -1)"
    fi

    if ! command -v jq &>/dev/null; then
        if command -v brew &>/dev/null; then
            brew install jq
        elif command -v apt-get &>/dev/null; then
            sudo apt-get install -y jq
        fi
    else
        ok "jq: $(jq --version)"
    fi
}

install_system_tools

# ─────────────────────────────────────────────────────────────────────────────
# 3. Python-зависимости
# ─────────────────────────────────────────────────────────────────────────────

log "Устанавливаю Python-зависимости..."

# Базовые (всегда)
pip install --quiet \
    tree-sitter==0.21.3 \
    networkx \
    pyyaml \
    lancedb \
    pyvis \
    tqdm \
    rich

ok "Базовые Python-пакеты установлены"

# Конфиг-парсеры (всегда полезны)
pip install --quiet \
    crossplane \
    dockerfile-parse

ok "crossplane (nginx), dockerfile-parse установлены"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Грамматики tree-sitter для найденных языков
# ─────────────────────────────────────────────────────────────────────────────

GRAMMARS_DIR="$SCRIPT_DIR/src/grammars"
mkdir -p "$GRAMMARS_DIR"

clone_grammar() {
    local repo_url="$1"
    local dir_name="$2"
    local dest="$GRAMMARS_DIR/$dir_name"
    if [ -d "$dest" ]; then
        ok "Грамматика уже есть: $dir_name"
    else
        log "Клонирую грамматику: $dir_name"
        git clone --depth=1 --quiet "$repo_url" "$dest" 2>/dev/null && ok "  $dir_name" || warn "  Не удалось: $repo_url"
    fi
}

# Java — всегда (основной язык train-ticket)
clone_grammar "https://github.com/tree-sitter/tree-sitter-java" "tree-sitter-java"

for lang in $DETECTED_LANGS; do
    case "$lang" in
        python)
            clone_grammar "https://github.com/tree-sitter/tree-sitter-python" "tree-sitter-python"
            ;;
        go)
            clone_grammar "https://github.com/tree-sitter/tree-sitter-go" "tree-sitter-go"
            ;;
        javascript)
            clone_grammar "https://github.com/tree-sitter/tree-sitter-javascript" "tree-sitter-javascript"
            clone_grammar "https://github.com/tree-sitter/tree-sitter-typescript" "tree-sitter-typescript"
            ;;
        rust)
            clone_grammar "https://github.com/tree-sitter/tree-sitter-rust" "tree-sitter-rust"
            ;;
        c_cpp)
            clone_grammar "https://github.com/tree-sitter/tree-sitter-c" "tree-sitter-c"
            clone_grammar "https://github.com/tree-sitter/tree-sitter-cpp" "tree-sitter-cpp"
            ;;
        php)
            clone_grammar "https://github.com/tree-sitter/tree-sitter-php" "tree-sitter-php"
            ;;
        ruby)
            clone_grammar "https://github.com/tree-sitter/tree-sitter-ruby" "tree-sitter-ruby"
            ;;
        kotlin)
            clone_grammar "https://github.com/nickel-lang/tree-sitter-kotlin" "tree-sitter-kotlin" || \
            clone_grammar "https://github.com/fwcd/tree-sitter-kotlin" "tree-sitter-kotlin"
            ;;
    esac
done

# ─────────────────────────────────────────────────────────────────────────────
# 5. Языко-специфичные инструменты (опциональные)
# ─────────────────────────────────────────────────────────────────────────────

install_optional() {
    local tool="$1"
    local install_cmd="$2"
    local check_cmd="${3:-$tool}"

    if command -v "$check_cmd" &>/dev/null; then
        ok "$tool уже установлен"
    else
        log "Устанавливаю $tool (опционально)..."
        eval "$install_cmd" 2>/dev/null && ok "$tool установлен" || warn "$tool: не удалось установить"
    fi
}

for lang in $DETECTED_LANGS; do
    case "$lang" in
        c_cpp)
            log "C/C++ обнаружен — устанавливаю cppcheck..."
            if command -v brew &>/dev/null; then
                install_optional "cppcheck" "brew install cppcheck"
            elif command -v apt-get &>/dev/null; then
                install_optional "cppcheck" "sudo apt-get install -y cppcheck"
            fi
            ;;
        php)
            log "PHP обнаружен — устанавливаю phpstan..."
            if command -v composer &>/dev/null; then
                install_optional "phpstan" "composer global require phpstan/phpstan"
            elif command -v npm &>/dev/null; then
                warn "PHP инструменты: установите phpstan через composer"
            fi
            ;;
        javascript)
            log "JS/TS обнаружен — устанавливаю eslint (если есть npm)..."
            if command -v npm &>/dev/null; then
                install_optional "eslint" "npm install -g eslint typescript" "eslint"
            fi
            ;;
    esac
done

# semgrep — универсальный, устанавливаем всегда
if ! command -v semgrep &>/dev/null; then
    log "Устанавливаю semgrep..."
    pip install --quiet semgrep && ok "semgrep установлен" || warn "semgrep: не удалось установить"
else
    ok "semgrep: $(semgrep --version 2>&1 | head -1)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 6. BGE-M3 модель (опционально, нужен Tier 3+)
# ─────────────────────────────────────────────────────────────────────────────

if [ "${INSTALL_EMBEDDINGS:-0}" = "1" ]; then
    log "Устанавливаю sentence-transformers (BGE-M3)..."
    pip install --quiet sentence-transformers torch --index-url https://download.pytorch.org/whl/cpu
    python -c "
from sentence_transformers import SentenceTransformer
print('Загружаю BGE-M3...')
SentenceTransformer('BAAI/bge-m3')
print('BGE-M3 готов')
" && ok "BGE-M3 загружен" || warn "BGE-M3: не удалось загрузить (запустите с INSTALL_EMBEDDINGS=1 позже)"
else
    log "Пропускаю BGE-M3 (запустите INSTALL_EMBEDDINGS=1 ./setup.sh для Tier 3+)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Итог
# ─────────────────────────────────────────────────────────────────────────────

echo ""
ok "═══════════════════════════════════════════════════"
ok "Установка завершена!"
ok "═══════════════════════════════════════════════════"
echo ""
log "Быстрая проверка:"
echo "  python -m blast_radius status"
echo "  python -m blast_radius index  --repo $REPO_PATH"
echo "  python -m blast_radius analyze --repo $REPO_PATH --change <файл> | jq ."
echo ""
log "Для семантического поиска (Tier 3):"
echo "  INSTALL_EMBEDDINGS=1 ./setup.sh $REPO_PATH"
