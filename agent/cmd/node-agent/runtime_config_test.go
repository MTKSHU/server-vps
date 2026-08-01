package main

import "testing"

func TestNormalizeCollectionConfigUsesFallbackAndBounds(t *testing.T) {
	fallback := AgentCollectionConfig{
		MetricsIntervalSeconds:   2,
		HeartbeatIntervalSeconds: 15,
		ContainerIntervalSeconds: 15,
		StorageIntervalSeconds:   60,
		InventoryIntervalSeconds: 300,
		TaskPollIntervalSeconds:  5,
	}
	got := normalizeCollectionConfig(AgentCollectionConfig{
		MetricsIntervalSeconds:   0,
		HeartbeatIntervalSeconds: 20,
		ContainerIntervalSeconds: 5,
		StorageIntervalSeconds:   99999,
		InventoryIntervalSeconds: 30,
		TaskPollIntervalSeconds:  0,
	}, fallback)
	if got.MetricsIntervalSeconds != 2 {
		t.Fatalf("metrics interval = %d", got.MetricsIntervalSeconds)
	}
	if got.ContainerIntervalSeconds != 20 {
		t.Fatalf("container interval = %d, want heartbeat boundary 20", got.ContainerIntervalSeconds)
	}
	if got.StorageIntervalSeconds != 3600 || got.InventoryIntervalSeconds != 60 {
		t.Fatalf("slow intervals = storage %d inventory %d", got.StorageIntervalSeconds, got.InventoryIntervalSeconds)
	}
	if got.TaskPollIntervalSeconds != 5 {
		t.Fatalf("task interval = %d", got.TaskPollIntervalSeconds)
	}
}

func TestRuntimeConfigStoreHotUpdate(t *testing.T) {
	store := newRuntimeConfigStore(AgentCollectionConfig{
		MetricsIntervalSeconds:   2,
		HeartbeatIntervalSeconds: 15,
		ContainerIntervalSeconds: 15,
		StorageIntervalSeconds:   60,
		InventoryIntervalSeconds: 300,
		TaskPollIntervalSeconds:  5,
	})
	updated := store.update(AgentCollectionConfig{
		MetricsIntervalSeconds:   3,
		HeartbeatIntervalSeconds: 30,
		ContainerIntervalSeconds: 30,
		StorageIntervalSeconds:   120,
		InventoryIntervalSeconds: 600,
		TaskPollIntervalSeconds:  10,
	})
	if updated.MetricsIntervalSeconds != 3 || store.get().HeartbeatIntervalSeconds != 30 {
		t.Fatalf("hot update not applied: %#v", store.get())
	}
}
