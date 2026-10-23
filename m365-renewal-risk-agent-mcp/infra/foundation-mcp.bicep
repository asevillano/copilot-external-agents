targetScope = 'resourceGroup'

// Isolated foundation for the -mcp version. The current bridge's environment,
// identity and app are not referenced or modified.
param location string = 'eastus'
param environmentName string = 'cae-renewal-risk-m365-mcp'
param registryName string
param mcpIdentityName string = 'id-renewal-risk-mcp-server'
param bridgeIdentityName string = 'id-renewal-risk-m365-mcp'

var tags = { purpose: 'm365-gcp-bridge-mcp' }

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource mcpIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: mcpIdentityName
  location: location
  tags: tags
}

resource bridgeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: bridgeIdentityName
  location: location
  tags: tags
}

resource mcpPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, mcpIdentity.id, 'AcrPull')
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: mcpIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource bridgePull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, bridgeIdentity.id, 'AcrPull')
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: bridgeIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
  }
}

output environmentId string = environment.id
output defaultDomain string = environment.properties.defaultDomain
output registryServer string = registry.properties.loginServer
output mcpIdentityId string = mcpIdentity.id
output mcpClientId string = mcpIdentity.properties.clientId
output mcpPrincipalId string = mcpIdentity.properties.principalId
output bridgeIdentityId string = bridgeIdentity.id
output bridgeClientId string = bridgeIdentity.properties.clientId
output bridgePrincipalId string = bridgeIdentity.properties.principalId
