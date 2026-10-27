targetScope = 'resourceGroup'

param environmentId string
param identityId string
param identityClientId string
param registryServer string
param image string
param gcpWorkloadIdentityAudience string
param gcpAgentResource string
param m365TenantId string
@allowed(['identity', 'backend'])
param mode string = 'identity'
@minValue(0)
@maxValue(1)
param minReplicas int = 1

var probeArgs = {
  identity: ['--identity-claims']
  backend: ['--backend']
}

// No ingress or bot credentials during operator-only identity/storage validation.
resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'ca-renewal-risk-m365'
  location: 'eastus'
  tags: { purpose: 'm365-gcp-bridge', phase: 'bootstrap' }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [{ server: registryServer, identity: identityId }]
    }
    template: {
      containers: [
        {
          name: 'bridge'
          image: image
          command: concat(['python', 'scripts/probe_aca.py'], probeArgs[mode], ['--hold-seconds', '3600'])
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
            { name: 'GCP_AZURE_TOKEN_RESOURCE', value: 'api://AzureADTokenExchange' }
            { name: 'GCP_WORKLOAD_IDENTITY_AUDIENCE', value: gcpWorkloadIdentityAudience }
            { name: 'GCP_AGENT_RESOURCE', value: gcpAgentResource }
            { name: 'M365_TENANT_ID', value: m365TenantId }
          ]
        }
      ]
      scale: { minReplicas: minReplicas, maxReplicas: 1 }
    }
  }
}
