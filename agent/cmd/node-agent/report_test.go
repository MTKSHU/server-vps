package main

import (
	"math"
	"testing"
	"time"
)

func TestParseCPUModelPrefersModelNameOverProcessorIndex(t *testing.T) {
	content := `
processor	: 0
vendor_id	: GenuineIntel
cpu family	: 6
model name	: Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz
`
	if got := parseCPUModelForTest(content); got != "Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz" {
		t.Fatalf("cpu model = %q", got)
	}
}

func TestParseCPUModelSupportsAMDModelName(t *testing.T) {
	content := `
processor	: 0
vendor_id	: AuthenticAMD
model name	: AMD Ryzen Threadripper PRO 5975WX 32-Cores
`
	if got := parseCPUModelForTest(content); got != "AMD Ryzen Threadripper PRO 5975WX 32-Cores" {
		t.Fatalf("cpu model = %q", got)
	}
}

func TestParseCPUModelSkipsNumericProcessorFallback(t *testing.T) {
	content := `
processor	: 0
vendor_id	: GenuineIntel
`
	if got := parseCPUModelForTest(content); got != "GenuineIntel" {
		t.Fatalf("cpu model = %q", got)
	}
}

func TestParseStorageSizeGB(t *testing.T) {
	cases := []struct {
		input string
		want  float64
	}{
		{"128GiB", 128},
		{"1.5 TiB", 1536},
		{"900MiB", 0.87890625},
		{"1099511627776 bytes", 1024},
	}
	for _, tc := range cases {
		if got := parseStorageSizeGB(tc.input); math.Abs(got-tc.want) > 0.000001 {
			t.Fatalf("parseStorageSizeGB(%q) = %f, want %f", tc.input, got, tc.want)
		}
	}
}

func TestParseIncusStorageInfoGB(t *testing.T) {
	output := `
info:
  description:
  driver: zfs
  space used: 72.25GiB
  total space: 1.5TiB
`
	total, used := parseIncusStorageInfoGB(output)
	if math.Abs(total-1536) > 0.000001 || math.Abs(used-72.25) > 0.000001 {
		t.Fatalf("parseIncusStorageInfoGB = total %f used %f, want total 1536 used 72.25", total, used)
	}
}

func TestParseNVIDIACudaVersionOutput(t *testing.T) {
	cases := []struct {
		name   string
		output string
		want   string
	}{
		{
			name:   "classic status header",
			output: `| NVIDIA-SMI 575.57.08  Driver Version: 575.57.08  CUDA Version: 12.9 |`,
			want:   "12.9",
		},
		{
			name:   "610 CUDA UMD version output",
			output: "NVIDIA-SMI version : 610.43.02\nCUDA UMD Version      : 13.3\n",
			want:   "13.3",
		},
		{
			name:   "missing version",
			output: "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.",
			want:   "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := parseNVIDIACudaVersionOutput(tc.output); got != tc.want {
				t.Fatalf("parseNVIDIACudaVersionOutput() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestRefreshDue(t *testing.T) {
	now := time.Unix(1_000, 0)
	if !refreshDue(false, time.Time{}, now, 300) {
		t.Fatal("first collection must refresh")
	}
	last := now.Add(-59 * time.Second)
	if refreshDue(true, last, now, 60) {
		t.Fatal("storage refreshed before its interval")
	}
	if !refreshDue(true, last, now.Add(time.Second), 60) {
		t.Fatal("storage did not refresh at its interval boundary")
	}
}

func TestParseDefaultRouteInterfaceUsesLowestMetric(t *testing.T) {
	routes := `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
eth1 00000000 0100000A 0003 0 0 200 00000000 0 0 0
br1 00000000 0100000A 0003 0 0 100 00000000 0 0 0
eth0 0001A8C0 00000000 0001 0 0 0 00FFFFFF 0 0 0
`
	if got := parseDefaultRouteInterface(routes); got != "br1" {
		t.Fatalf("default route interface = %q, want br1", got)
	}
}

func TestCalculateNetworkRates(t *testing.T) {
	previous := networkCounters{Interface: "br1", RXBytes: 1000, TXBytes: 2000, SampledAt: time.Unix(10, 0)}
	current := networkCounters{Interface: "br1", RXBytes: 5000, TXBytes: 3000, SampledAt: time.Unix(12, 0)}
	rxRate, txRate := calculateNetworkRates(previous, current)
	if rxRate != 2000 || txRate != 500 {
		t.Fatalf("network rates = (%f, %f), want (2000, 500)", rxRate, txRate)
	}
	current.RXBytes = 10
	if rxRate, txRate = calculateNetworkRates(previous, current); rxRate != 0 || txRate != 0 {
		t.Fatalf("counter reset rates = (%f, %f), want zero", rxRate, txRate)
	}
}

func TestParseIncusInventoryAllowsEmptySuccessfulResult(t *testing.T) {
	containers, err := parseIncusContainers("")
	if err != nil || len(containers) != 0 {
		t.Fatalf("empty containers = %#v, %v", containers, err)
	}
	images, err := parseIncusImages("")
	if err != nil || len(images) != 0 {
		t.Fatalf("empty images = %#v, %v", images, err)
	}
}

func TestParseIncusInventory(t *testing.T) {
	containers, err := parseIncusContainers("demo,RUNNING,10.0.0.2 (eth0)\n")
	if err != nil || len(containers) != 1 || containers[0].Name != "demo" || containers[0].IP != "10.0.0.2" {
		t.Fatalf("containers = %#v, %v", containers, err)
	}
	images, err := parseIncusImages("ubuntu,abcdef,Ubuntu 24.04,x86_64\n")
	if err != nil || len(images) != 1 || images[0].Fingerprint != "abcdef" {
		t.Fatalf("images = %#v, %v", images, err)
	}
}
