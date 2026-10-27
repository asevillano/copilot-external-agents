targetScope = 'resourceGroup'

// MCP adapter for the GCP Renewal Risk Analyst: internal ingress only, reachable
// from apps in the same environment. Its identity is the only one federated to GCP.
param location string = 'eastus'
param appName string = 'ca-renewal-risk-mcp-server'
param environmentId string
param defaultDomain string
param registryServer string
param image string
param identityId string
param identityClientId string
param tenantId string
param mcpApiAppId string
param allowedClientIds string
param gcpAgentResource string
param gcpWorkloadIdentityAudience string
@minValue(0)
@maxValue(1)
param minReplicas int = 1

var internalHost = '${appName}.internal.${defaultDomain}'

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: { purpose: 'm365-gcp-bridge-mcp', component: 'mcp-server' }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        { server: registryServer, identity: identityId }
      ]
    }
    template: {
      containers: [
        {
          name: 'mcp'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            { type: 'Startup', httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }, periodSeconds: 5, failureThreshold: 30 }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }, periodSeconds: 10 }
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }, periodSeconds: 30, failureThreshold: 3 }
          ]
          env: [
            { name: 'PORT', value: '8080' }
            { name: 'HOST', value: '0.0.0.0' }
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
            { name: 'GCP_AGENT_RESOURCE', value: gcpAgentResource }
            { name: 'GCP_WORKLOAD_IDENTITY_AUDIENCE', value: gcpWorkloadIdentityAudience }
            { name: 'GCP_AZURE_TOKEN_RESOURCE', value: 'api://AzureADTokenExchange' }
            { name: 'ENTRA_TENANT_ID', value: tenantId }
            { name: 'MCP_AUDIENCES', value: 'api://${mcpApiAppId},${mcpApiAppId}' }
            { name: 'MCP_ALLOWED_CLIENT_IDS', value: allowedClientIds }
            { name: 'MCP_RESOURCE_URL', value: 'https://${internalHost}/mcp' }
            { name: 'MCP_ALLOWED_HOSTS', value: '${internalHost},${internalHost}:*,${appName},${appName}:*' }
          ]
        }
      ]
      // one replica: the Agent Engine session cache and per-conversation locks are in-process
      scale: {
        minReplicas: minReplicas
        maxReplicas: 1
        rules: [
          { name: 'http', http: { metadata: { concurrentRequests: '10' } } }
        ]
      }
    }
  }
}

output internalUrl string = 'https://${internalHost}/mcp'
output resourceId string = app.id
