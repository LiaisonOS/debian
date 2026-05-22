# Author      : Gaston Gonzalez
# Date        : 23 April 2024
# Modified    : Sylvain Deguire (VA2OPS), March 2026
# Description : bash profile

# All user accounts should be in the 'et-data' group to allow data sharing with
# LiaisonOS. A umask of 002 is required to allow users and the system to
# read and write shared data files.
umask 002

# Disable the terminal bell for opsec
xset b off

alias pbcopy='xsel --clipboard --input'
alias pbpaste='xsel --clipboard --output'

YELLOW='\x1B[1;33m'
NC='\x1B[0m'

export PS1="\[\e]0;\u@\h: \w\a\]${debian_chroot:+($debian_chroot)}\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\n\$ "

echo -e "${YELLOW}"
cat /etc/motd
echo -e "${NC}"

export ET_USER_CONFIG=${HOME}/.config/liaisonos/user.json
export ET_LOG_DIR=${HOME}/.local/share/emcomm-tools

# Export environment variables to support WINE installs for VARA
export WINEARCH="win32"
export WINEPREFIX="$HOME/.wine32"
export WINEDEBUG=-all
