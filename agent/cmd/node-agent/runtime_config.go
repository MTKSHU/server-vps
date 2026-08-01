package main

import "sync"

type runtimeConfigStore struct {
	mu     sync.RWMutex
	config AgentCollectionConfig
}

func clampInterval(value, fallback, minimum, maximum int) int {
	if value == 0 {
		value = fallback
	}
	if value < minimum {
		return minimum
	}
	if value > maximum {
		return maximum
	}
	return value
}

func normalizeCollectionConfig(config AgentCollectionConfig, fallback AgentCollectionConfig) AgentCollectionConfig {
	config.MetricsIntervalSeconds = clampInterval(config.MetricsIntervalSeconds, fallback.MetricsIntervalSeconds, 1, 60)
	config.HeartbeatIntervalSeconds = clampInterval(config.HeartbeatIntervalSeconds, fallback.HeartbeatIntervalSeconds, 5, 300)
	config.ContainerIntervalSeconds = clampInterval(config.ContainerIntervalSeconds, fallback.ContainerIntervalSeconds, 5, 300)
	if config.ContainerIntervalSeconds < config.HeartbeatIntervalSeconds {
		config.ContainerIntervalSeconds = config.HeartbeatIntervalSeconds
	}
	config.StorageIntervalSeconds = clampInterval(config.StorageIntervalSeconds, fallback.StorageIntervalSeconds, 30, 3600)
	config.InventoryIntervalSeconds = clampInterval(config.InventoryIntervalSeconds, fallback.InventoryIntervalSeconds, 60, 86400)
	config.TaskPollIntervalSeconds = clampInterval(config.TaskPollIntervalSeconds, fallback.TaskPollIntervalSeconds, 1, 60)
	return config
}

func newRuntimeConfigStore(config AgentCollectionConfig) *runtimeConfigStore {
	return &runtimeConfigStore{config: config}
}

func (store *runtimeConfigStore) get() AgentCollectionConfig {
	store.mu.RLock()
	defer store.mu.RUnlock()
	return store.config
}

func (store *runtimeConfigStore) update(config AgentCollectionConfig) AgentCollectionConfig {
	store.mu.Lock()
	defer store.mu.Unlock()
	store.config = normalizeCollectionConfig(config, store.config)
	return store.config
}
