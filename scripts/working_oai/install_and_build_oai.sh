#!/usr/bin/env bash
# ==============================================================================
# install_and_build_oai.sh — Clone and build OpenAirInterface (OAI) 4G LTE stack
# on POWDER nodes:
#   pc802 (eNB host)  &  pc801 (UE host)
# ==============================================================================

set -euo pipefail

KEY="${HOME}/.ssh/id_ed25519"
SSH_OPTS="-i ${KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"
GNB_HOST="saish@pc802.emulab.net"
UE_HOST="saish@pc801.emulab.net"

OAI_GIT_URL="https://gitlab.eurecom.fr/oai/openairinterface5g.git"
OAI_TAG="2024.w44" # or develop / v2.1.0
OAI_DIR="/opt/openairinterface5g"

echo "===================================================================="
echo " [1/2] Installing dependencies and building OAI 4G eNB on pc802..."
echo "===================================================================="

ssh ${SSH_OPTS} "${GNB_HOST}" "bash -s" << 'EOF'
set -euo pipefail
sudo apt-get update
sudo apt-get install -y git subversion libboost-all-dev libzmq3-dev libtool autoconf \
    automake build-essential libatlas-base-dev libblas-dev liblapack-dev liblapacke-dev \
    libconfig-dev libsctp-dev cmake ninja-build ccache

if [ ! -d "/opt/openairinterface5g" ]; then
    sudo git clone https://gitlab.eurecom.fr/oai/openairinterface5g.git /opt/openairinterface5g
fi

cd /opt/openairinterface5g
sudo git fetch --all --tags
sudo git checkout 2024.w44 || sudo git checkout develop

# Install OAI build prerequisites
source oaienv
cd cmake_targets
sudo ./build_oai -I --install-optional-packages

# Build LTE softmodem with rfsimulator and telnetsrv
sudo ./build_oai -w SIMU --eNB --telnetsrv --ninja -c
sudo cp /opt/openairinterface5g/cmake_targets/ran_build/build/lte-softmodem /usr/local/bin/lte-softmodem || true

echo "=== pc802 OAI eNB build complete ==="
which lte-softmodem || ls -la /opt/openairinterface5g/cmake_targets/ran_build/build/lte-softmodem
EOF

echo "===================================================================="
echo " [2/2] Installing dependencies and building OAI 4G UE on pc801..."
echo "===================================================================="

ssh ${SSH_OPTS} "${UE_HOST}" "bash -s" << 'EOF'
set -euo pipefail
sudo apt-get update
sudo apt-get install -y git subversion libboost-all-dev libzmq3-dev libtool autoconf \
    automake build-essential libatlas-base-dev libblas-dev liblapack-dev liblapacke-dev \
    libconfig-dev libsctp-dev cmake ninja-build ccache

if [ ! -d "/opt/openairinterface5g" ]; then
    sudo git clone https://gitlab.eurecom.fr/oai/openairinterface5g.git /opt/openairinterface5g
fi

cd /opt/openairinterface5g
sudo git fetch --all --tags
sudo git checkout 2024.w44 || sudo git checkout develop

# Install OAI build prerequisites
source oaienv
cd cmake_targets
sudo ./build_oai -I --install-optional-packages

# Build LTE UE softmodem with rfsimulator
sudo ./build_oai -w SIMU --UE --ninja -c
sudo cp /opt/openairinterface5g/cmake_targets/ran_build/build/lte-uesoftmodem /usr/local/bin/lte-uesoftmodem || true

echo "=== pc801 OAI UE build complete ==="
which lte-uesoftmodem || ls -la /opt/openairinterface5g/cmake_targets/ran_build/build/lte-uesoftmodem
EOF

echo "===================================================================="
echo " OAI 4G LTE build process finished on both nodes."
echo "===================================================================="
