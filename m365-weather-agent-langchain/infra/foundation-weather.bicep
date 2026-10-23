targetScope = 'resourceGroup'

// Third version: LangChain weather agent and its bridge in SEPARATE ACA environments.
// Nothing from the renewal-risk versions is referenced or modified.
param location string = 'eastus'
param agentEnvironmentName string = 'cae-weather-agents'
param bridgeEnvironmentName string = 'cae-weather-m365-bridge'
param registryName string
param foundryName string
param agentIdentityName string = 'id-weather-agent'
param bridgeIdentityName string = 'id-weather-m365-bridge'

var tags = { purpose: 'm365-weather-agent-langchain' }

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryName
}

resource agentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: agentIdentityName
  location: location
  tags: tags
}

resource bridgeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: bridgeIdentityName
  location: location
  tags: tags
}

var acrPull = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var openAiUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

resource agentPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, agentIdentity.id, 'AcrPull')
  scope: registry
  properties: { roleDefinitionId: acrPull, principalId: agentIdentity.properties.principalId, principalType: 'ServicePrincipal' }
}

resource bridgePull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, bridgeIdentity.id, 'AcrPull')
  scope: registry
  properties: { roleDefinitionId: acrPull, principalId: bridgeIdentity.properties.principalId, principalType: 'ServicePrincipal' }
}

// only the agent can call the model; the bridge has no model access
resource agentModel 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, agentIdentity.id, 'CognitiveServicesOpenAIUser')
  scope: foundry
  properties: { roleDefinitionId: openAiUser, principalId: agentIdentity.properties.principalId, principalType: 'ServicePrincipal' }
}

resource agentEnv 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: agentEnvironmentName
  location: location
  tags: tags
  properties: { workloadProfiles: [ { name: 'Consumption', workloadProfileType: 'Consumption' } ] }
}

resource bridgeEnv 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: bridgeEnvironmentName
  location: location
  tags: tags
  properties: { workloadProfiles: [ { name: 'Consumption', workloadProfileType: 'Consumption' } ] }
}

output agentEnvironmentId string = agentEnv.id
output agentDefaultDomain string = agentEnv.properties.defaultDomain
output bridgeEnvironmentId string = bridgeEnv.id
output registryServer string = registry.properties.loginServer
output openAiEndpoint string = 'https://${foundryName}.openai.azure.com/'
output agentIdentityId string = agentIdentity.id
output agentClientId string = agentIdentity.properties.clientId
output agentPrincipalId string = agentIdentity.properties.principalId
output bridgeIdentityId string = bridgeIdentity.id
output bridgeClientId string = bridgeIdentity.properties.clientId
output bridgePrincipalId string = bridgeIdentity.properties.principalId
