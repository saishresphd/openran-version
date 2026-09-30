#!/bin/bash
# install_srsran_v25.sh — Build srsRAN 5G (v25) / Project Version from source with ZMQ
# Run on: gnb1 (pc802), uehost1 (pc801)
set -e

echo "=== Installing build dependencies ==="
sudo apt update
sudo apt install -y \
  cmake make gcc g++ pkg-config \
  libfftw3-dev libmbedtls-dev libsctp-dev \
  libconfig++-dev libboost-program-options-dev \
  libzmq3-dev libuhd-dev uhd-host \
  python3 git libyaml-cpp-dev

echo "=== Cloning and Checking out release_25_10 ==="
sudo rm -rf /opt/srsRAN_v25
sudo git clone https://github.com/srsran/srsRAN_4G.git /opt/srsRAN_v25
cd /opt/srsRAN_v25
sudo git checkout release_25_10

echo "=== Building srsRAN v25.10 with ZMQ ==="
sudo mkdir -p build && cd build
sudo cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_WERROR=OFF \
  -DENABLE_ZMQ=ON \
  -DENABLE_UHD=OFF

sudo make -j$(nproc) srsenb srsue srsran_rf_zmq

echo "=== Verifying ==="
./srsenb/src/srsenb --version || true
./srsue/src/srsue --version || true
echo "✓ srsRAN v25.10 built successfully at /opt/srsRAN_v25/build"
