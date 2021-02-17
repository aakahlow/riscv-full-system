#Copyright (c) 2021 The Regents of the University of California.
#All Rights Reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

import m5
from m5.objects import *
from m5.util import convert
from .caches import *

'''
This class creates a bare bones RISCV full system.

The targeted system is  based on SiFive FU540-C000.
Reference:
[1] https://sifive.cdn.prismic.io/sifive/b5e7a29c-
d3c2-44ea-85fb-acc1df282e21_FU540-C000-v1p3.pdf
'''

class RiscvSystem(System):

    def __init__(self, bbl, disk, cpu_type, num_cpus):
        super(RiscvSystem, self).__init__()

        # Set up the clock domain and the voltage domain
        self.clk_domain = SrcClockDomain()
        self.clk_domain.clock = '3GHz'
        self.clk_domain.voltage_domain = VoltageDomain()

        # DDR memory range starts from base address 0x80000000
        # based on [1]
        self.mem_ranges = [AddrRange(start=0x80000000, size='256MB')]

        # Create the main memory bus
        # This connects to main memory
        self.membus = SystemXBar(width = 64) # 64-byte width
        #self.membus.badaddr_responder = BadAddr()
        #self.membus.default = Self.badaddr_responder.pio

        # Set up the system port for functional access from the simulator
        self.system_port = self.membus.cpu_side_ports

        # Create the CPUs for our system.
        self.createCPU(cpu_type, num_cpus)

        self.workload = RiscvBareMetal()

        # this is user passed berkeley boot loader binary
        # currently the Linux payload is compiled into this
        # as well
        self.workload.bootloader = bbl

        # HiFive platform
        # This is based on a HiFive RISCV board and has
        # only a limited number of devices so far i.e.
        # PLIC, CLINT, UART, VirtIOMMIO
        self.platform = HiFive()

        # create and intialize devices currently supported for RISCV
        self.initDevices(self.membus, disk)

        # Create the cache heirarchy for the system.
        self.createCacheHierarchy()

        # Create the memory controller
        self.createMemoryControllerDDR3()

        self.setupInterrupts()

    def createCPU(self, cpu_type, num_cpus):
        if cpu_type == "atomic":
            self.cpu = [AtomicSimpleCPU(cpu_id = i)
                              for i in range(num_cpus)]
            self.mem_mode = 'atomic'
        elif cpu_type == "simple":
            self.cpu = [TimingSimpleCPU(cpu_id = i)
                        for i in range(num_cpus)]
            self.mem_mode = 'timing'
        else:
            m5.fatal("No CPU type {}".format(cpu_type))

        for cpu in self.cpu:
            cpu.createThreads()

    def createCacheHierarchy(self):
        for cpu in self.cpu:
            # Create a memory bus, a coherent crossbar, in this case
            cpu.l2bus = L2XBar()

            # Create an L1 instruction and data cache
            cpu.icache = L1ICache()
            cpu.dcache = L1DCache()
            cpu.mmucache = MMUCache()

            # Connect the instruction and data caches to the CPU
            cpu.icache.connectCPU(cpu)
            cpu.dcache.connectCPU(cpu)
            cpu.mmucache.connectCPU(cpu)

            # Hook the CPU ports up to the l2bus
            cpu.icache.connectBus(cpu.l2bus)
            cpu.dcache.connectBus(cpu.l2bus)
            cpu.mmucache.connectBus(cpu.l2bus)

            # Create an L2 cache and connect it to the l2bus
            cpu.l2cache = L2Cache()
            cpu.l2cache.connectCPUSideBus(cpu.l2bus)

            # Connect the L2 cache to the L3 bus
            cpu.l2cache.connectMemSideBus(self.membus)

    def setupInterrupts(self):
        for cpu in self.cpu:
            # create the interrupt controller CPU and connect to the membus
            cpu.createInterruptController()


    def createMemoryControllerDDR3(self):
        self.mem_cntrls = [
            MemCtrl(dram = DDR3_1600_8x8(range = self.mem_ranges[0]),
                    port = self.membus.mem_side_ports)
        ]

    def initDevices(self, membus, disk):

        self.iobus = IOXBar()
        self.intrctrl = IntrControl()

        # Set the frequency of RTC (real time clock) used by
        # CLINT (core level interrupt controller).
        # This frequency is 1MHz in SiFive's U54MC.
        # Setting it to 100MHz for faster simulation (from riscv/fs_linux.py)
        self.platform.rtc = RiscvRTC(frequency=Frequency("100MHz"))

        # RTC sends the clock signal to CLINT via an interrupt pin.
        self.platform.clint.int_pin = self.platform.rtc.int_pin

        # VirtIOMMIO
        image = CowDiskImage(child=RawDiskImage(read_only=True), read_only=False)
        image.child.image_file = disk
        self.platform.disk = MmioVirtIO(
            vio=VirtIOBlock(image=image),
            interrupt_id=0x8,
            pio_size = 4096,
            pio_addr=0x10008000
        )

        # From riscv/fs_linux.py

        uncacheable_range = [
            *self.platform._on_chip_ranges(),
            *self.platform._off_chip_ranges()
        ]
        pma_checker =  PMAChecker(uncacheable=uncacheable_range)

        # PMA checker can be defined at system-level (system.pma_checker)
        # or MMU-level (system.cpu[0].mmu.pma_checker). It will be resolved
        # by RiscvTLB's Parent.any proxy
        for cpu in self.cpu:
            cpu.mmu.pma_checker = pma_checker

        self.bridge = Bridge(delay='50ns')
        self.bridge.master = self.iobus.slave
        self.bridge.slave = self.membus.master
        self.bridge.ranges = self.platform._off_chip_ranges()

        self.platform.attachOnChipIO(self.membus)
        self.platform.attachOffChipIO(self.iobus)

        # Attach the PLIC (platform level interrupt controller)
        # to the platform
        self.platform.attachPlic()