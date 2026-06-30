package main

import (
	"math"
	"testing"
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
