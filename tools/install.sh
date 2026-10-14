#!/usr/bin/env bash
# Installs pinned tools from tools/versions.json. Every download is checked
# against the SHA-256 in that file before it is unpacked or made executable.
#
#   tools/install.sh jdk <dir>           Temurin JDK into <dir>
#   tools/install.sh maven               Maven distribution into the directory
#                                        where app/mvnw looks for it
#   tools/install.sh tool <name> <dir>   binary <name> into <dir>
#
# Needs bash, curl, jq, sha256sum, tar and unzip. No retries: a failed
# download fails the run.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
versions="$here/versions.json"
wrapper_properties="$here/../app/.mvn/wrapper/maven-wrapper.properties"

die() {
  printf 'install.sh: %s\n' "$1" >&2
  exit 1
}

# fetch <url> <sha256> <file>
fetch() {
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --output "$3" "$1"
  printf '%s  %s\n' "$2" "$3" | sha256sum --check --strict --quiet - ||
    die "SHA-256 mismatch for $1"
}

# Copy of hash_string from app/mvnw (Maven Wrapper 3.3.4): Java String::hashCode
# of the distribution URL, printed in hex.
hash_string() {
  str="${1:-}" h=0
  while [ -n "$str" ]; do
    char="${str%"${str#?}"}"
    h=$(((h * 31 + $(LC_CTYPE=C printf %d "'$char")) % 4294967296))
    str="${str#?}"
  done
  printf %x\\n $h
}

install_jdk() {
  local dir=$1 url sha tmp
  url=$(jq -er '.jdk.url' "$versions")
  sha=$(jq -er '.jdk.sha256' "$versions")
  [[ ! -e $dir ]] || die "$dir exists"
  tmp=$(mktemp -d)
  fetch "$url" "$sha" "$tmp/jdk.tar.gz"
  mkdir -p "$dir"
  tar -xzf "$tmp/jdk.tar.gz" -C "$dir" --strip-components=1
  rm -rf "$tmp"
  "$dir/bin/java" -version
}

install_maven() {
  local url sha wrapper_url name home tmp extracted
  url=$(jq -er '.maven.url' "$versions")
  sha=$(jq -er '.maven.sha256' "$versions")
  wrapper_url=$(sed -n 's/^distributionUrl=//p' "$wrapper_properties" | tr -d '[:space:]')
  [[ $url == "$wrapper_url" ]] ||
    die "maven.url in versions.json differs from distributionUrl in $wrapper_properties"
  [[ -z ${MVNW_REPOURL-} ]] || die "MVNW_REPOURL is set, mvnw would use another URL"
  # Directory as computed by app/mvnw: MAVEN_HOME pattern
  # ~/.m2/wrapper/dists/apache-maven-<version>/<hash of the URL>
  name=${url##*/}
  name=${name%.*}
  name=${name%-bin}
  home="${MAVEN_USER_HOME:-$HOME/.m2}/wrapper/dists/$name/$(hash_string "$url")"
  [[ ! -e $home ]] || die "$home exists"
  tmp=$(mktemp -d)
  fetch "$url" "$sha" "$tmp/maven.zip"
  unzip -q "$tmp/maven.zip" -d "$tmp/x"
  extracted=$(find "$tmp/x" -mindepth 1 -maxdepth 1 -type d)
  [[ $(printf '%s\n' "$extracted" | wc -l) -eq 1 ]] || die "unexpected layout of $url"
  mkdir -p "$(dirname "$home")"
  mv "$extracted" "$home"
  rm -rf "$tmp"
  printf 'Maven installed at %s\n' "$home"
}

install_tool() {
  local name=$1 dir=$2 entry url sha tmp
  entry=$(jq -ec --arg n "$name" '.tools[$n] // .support_tools[$n]' "$versions") ||
    die "no tool $name in versions.json"
  url=$(jq -er '.url' <<<"$entry")
  sha=$(jq -er '.sha256' <<<"$entry")
  mkdir -p "$dir"
  [[ ! -e $dir/$name ]] || die "$dir/$name exists"
  tmp=$(mktemp -d)
  case $url in
  *.tar.gz)
    fetch "$url" "$sha" "$tmp/archive.tar.gz"
    tar -xzf "$tmp/archive.tar.gz" -C "$tmp" "$name"
    ;;
  *)
    fetch "$url" "$sha" "$tmp/$name"
    ;;
  esac
  install -m 0755 "$tmp/$name" "$dir/$name"
  rm -rf "$tmp"
  printf '%s %s installed at %s\n' "$name" "$(jq -r '.version' <<<"$entry")" "$dir/$name"
}

case ${1-} in
jdk) install_jdk "${2:?usage: install.sh jdk <dir>}" ;;
maven) install_maven ;;
tool) install_tool "${2:?usage: install.sh tool <name> <dir>}" "${3:?usage: install.sh tool <name> <dir>}" ;;
*) die "usage: install.sh jdk <dir> | maven | tool <name> <dir>" ;;
esac
