# Copyright (c) 2026 DisplayLink (UK) Ltd.
FROM quay.io/centos/centos:stream9
MAINTAINER Synaptics Technical Support <technical-enquiries@synaptics.com>

RUN dnf install -y epel-release && dnf clean all

# Install developer convenience tools (vim, tig, kmod, dkms from EPEL)
RUN dnf install -y make git vim tig kmod dkms && dnf clean all

# Install linux kernel build dependencies
RUN dnf install -y bc bison flex elfutils-libelf-devel openssl-devel && dnf clean all

# C++ gcc and gcc crosscompilers
RUN dnf install -y gcc gcc-c++ && dnf clean all

RUN dnf install -y gawk diffutils dwarves elfutils-libelf gcc kernel kernel-devel make openssl patch crypto-policies-scripts && dnf clean all
RUN update-crypto-policies --set LEGACY

# Fetch linux source code, make the repo usable by multiple users and mark it safe for Git
RUN mkdir -p /usr/src && cd /usr/src && git clone -b main https://gitlab.com/redhat/centos-stream/src/kernel/centos-stream-9.git linux && \
  cd /usr/src/linux && git config core.sharedRepository all && git fetch --tags && \
  chown -R root:root /usr/src/linux && \
  chmod -R a+rwX /usr/src/linux && \
  find /usr/src/linux -type d -exec chmod g+s {} + && \
  git -C /usr/src/linux config core.sharedRepository all && \
  git config --system --add safe.directory /usr/src/linux
