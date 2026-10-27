targetScope = 'resourceGroup'

@minLength(3)
@maxLength(10)
@description('Lowercase alphanumeric prefix used for resource names.')
param namePrefix string = 'game'

@description('Azure region for the development environment.')
param location string = resourceGroup().location

@description('Azure region for PostgreSQL when the primary region has service restrictions.')
param postgresLocation string = 'centralus'

@description('Optional Microsoft Entra group object IDs granted AKS cluster-admin access.')
param aksAdminGroupObjectIds array = []

@secure()
@minLength(12)
@description('Initial PostgreSQL administrator password. Supply securely at deployment time.')
param postgresAdminPassword string

var suffix = substring(uniqueString(resourceGroup().id), 0, 6)
var aksName = 'aks-${namePrefix}-${suffix}'
var acrName = 'acr${namePrefix}${suffix}'
var postgresName = 'pg-${namePrefix}-${suffix}'
var redisName = 'redis-${namePrefix}-${suffix}'
var foundryName = 'ai-${namePrefix}-${suffix}'
var foundryProjectName = 'ops'
var postgresPrivateDnsName = 'private.postgres.database.azure.com'

resource publicIp 'Microsoft.Network/publicIPAddresses@2023-09-01' = {
  name: 'pip-${namePrefix}-${suffix}'
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2023-09-01' = {
  name: 'nat-${namePrefix}-${suffix}'
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [
      {
        id: publicIp.id
      }
    ]
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: 'vnet-${namePrefix}-${suffix}'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.20.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'aks'
        properties: {
          addressPrefix: '10.20.0.0/22'
          natGateway: {
            id: natGateway.id
          }
        }
      }
      {
        name: 'postgres'
        properties: {
          addressPrefix: '10.20.4.0/24'
          delegations: [
            {
              name: 'postgres-flexible-server'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.20.5.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource postgresVirtualNetwork 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: 'vnet-pg-${namePrefix}-${suffix}'
  location: postgresLocation
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.21.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'postgres'
        properties: {
          addressPrefix: '10.21.4.0/24'
          delegations: [
            {
              name: 'postgres-flexible-server'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

resource postgresPrivateDns 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: postgresPrivateDnsName
  location: 'global'
}

resource postgresDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: postgresPrivateDns
  name: 'link-${namePrefix}-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource postgresServerDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: postgresPrivateDns
  name: 'link-pg-${namePrefix}-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: postgresVirtualNetwork.id
    }
  }
}

resource aksToPostgresPeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2023-09-01' = {
  parent: virtualNetwork
  name: 'to-postgres'
  properties: {
    remoteVirtualNetwork: {
      id: postgresVirtualNetwork.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource postgresToAksPeering 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2023-09-01' = {
  parent: postgresVirtualNetwork
  name: 'to-aks'
  properties: {
    remoteVirtualNetwork: {
      id: virtualNetwork.id
    }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: false
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource aksControlIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-aks-control-${namePrefix}-${suffix}'
  location: location
}

resource aksKubeletIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-aks-kubelet-${namePrefix}-${suffix}'
  location: location
}

resource aksNetworkRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(virtualNetwork.id, aksControlIdentity.id, 'network-contributor')
  scope: virtualNetwork
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4d97b98b-1d4f-4787-a291-c67834d212e7'
    )
    principalId: aksControlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource aksKubeletIdentityRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aksKubeletIdentity.id, aksControlIdentity.id, 'managed-identity-operator')
  scope: aksKubeletIdentity
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'f1a07417-d97a-45cb-824c-7a7467783830'
    )
    principalId: aksControlIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, aksKubeletIdentity.id, 'acr-pull')
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
    principalId: aksKubeletIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource aks 'Microsoft.ContainerService/managedClusters@2025-10-01' = {
  name: aksName
  location: location
  sku: {
    name: 'Base'
    tier: 'Free'
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${aksControlIdentity.id}': {}
    }
  }
  properties: {
    dnsPrefix: 'dns-${namePrefix}-${suffix}'
    enableRBAC: true
    disableLocalAccounts: true
    aadProfile: {
      managed: true
      enableAzureRBAC: true
      adminGroupObjectIDs: aksAdminGroupObjectIds
    }
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    identityProfile: {
      kubeletidentity: {
        resourceId: aksKubeletIdentity.id
      }
    }
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        count: 1
        vmSize: 'Standard_D2as_v5'
        osType: 'Linux'
        osSKU: 'Ubuntu'
        type: 'VirtualMachineScaleSets'
        vnetSubnetID: resourceId(
          'Microsoft.Network/virtualNetworks/subnets',
          virtualNetwork.name,
          'aks'
        )
        maxPods: 30
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkPolicy: 'azure'
      outboundType: 'userAssignedNATGateway'
      serviceCidr: '10.0.0.0/16'
      dnsServiceIP: '10.0.0.10'
    }
  }
  dependsOn: [
    aksNetworkRole
    aksKubeletIdentityRole
    acrPullRole
  ]
}

resource foundryAgentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-foundry-agent-${namePrefix}-${suffix}'
  location: location
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${foundryAgentIdentity.id}': {}
    }
  }
  sku: {
    name: 'S0'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: foundryName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: false
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: foundryProjectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'Read-only AKS incident investigation tools'
    displayName: 'AKS incident operations'
  }
  dependsOn: [
    foundryModel
  ]
}

resource foundryModel 'Microsoft.CognitiveServices/accounts/deployments@2023-05-01' = {
  parent: foundry
  name: 'gpt-4.1-mini'
  sku: {
    name: 'GlobalStandard'
    capacity: 1
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4.1-mini'
      version: '2025-04-14'
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

resource foundryOpenAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, foundryAgentIdentity.id, 'cognitive-services-openai-user')
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'a001fd3d-188f-4b5d-821b-7da978bf7442'
    )
    principalId: foundryAgentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource agentFederatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: foundryAgentIdentity
  name: 'aks-incident-agent'
  properties: {
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:incident-agent:incident-agent'
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: postgresLocation
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    administratorLogin: 'gameadmin'
    administratorLoginPassword: postgresAdminPassword
    version: '16'
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      delegatedSubnetResourceId: resourceId(
        'Microsoft.Network/virtualNetworks/subnets',
        postgresVirtualNetwork.name,
        'postgres'
      )
      privateDnsZoneArmResourceId: postgresPrivateDns.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      storageSizeGB: 32
      autoGrow: 'Disabled'
    }
  }
  dependsOn: [
    postgresDnsLink
    postgresServerDnsLink
    aksToPostgresPeering
    postgresToAksPeering
  ]
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'game'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource redis 'Microsoft.Cache/redisEnterprise@2026-10-01-preview' = {
  name: redisName
  location: location
  sku: {
    name: 'Balanced_B0'
  }
  properties: {
    encryption: {}
    highAvailability: 'Disabled'
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
  }
}

resource redisPrivateDns 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.redis.azure.net'
  location: 'global'
}

resource redisPrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: redisPrivateDns
  name: 'link-${namePrefix}-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource redisPrivateEndpoint 'Microsoft.Network/privateEndpoints@2023-09-01' = {
  name: 'pe-${redisName}'
  location: location
  properties: {
    subnet: {
      id: resourceId(
        'Microsoft.Network/virtualNetworks/subnets',
        virtualNetwork.name,
        'private-endpoints'
      )
    }
    privateLinkServiceConnections: [
      {
        name: 'redis'
        properties: {
          privateLinkServiceId: redis.id
          groupIds: [
            'redisEnterprise'
          ]
        }
      }
    ]
  }
  dependsOn: [
    redisDatabase
  ]
}

resource redisPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-09-01' = {
  parent: redisPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'redis'
        properties: {
          privateDnsZoneId: redisPrivateDns.id
        }
      }
    ]
  }
  dependsOn: [
    redisPrivateDnsLink
  ]
}

resource redisDatabase 'Microsoft.Cache/redisEnterprise/databases@2026-10-01-preview' = {
  parent: redis
  name: 'default'
  properties: {
    accessKeysAuthentication: 'Enabled'
    clientProtocol: 'Encrypted'
    clusteringPolicy: 'EnterpriseCluster'
    evictionPolicy: 'VolatileLRU'
    modules: []
    port: 10000
  }
}

output aksName string = aks.name
output aksResourceId string = aks.id
output acrName string = registry.name
output acrLoginServer string = registry.properties.loginServer
output postgresName string = postgres.name
output postgresLocation string = postgres.location
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output postgresAdmin string = postgres.properties.administratorLogin
output redisName string = redis.name
output redisHostName string = redis.properties.hostName
output redisPort int = redisDatabase.properties.port
output redisPrivateEndpointId string = redisPrivateEndpoint.id
output aksOutboundIp string = publicIp.properties.ipAddress
output foundryName string = foundry.name
output foundryProjectName string = foundryProject.name
output foundryProjectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${foundryProject.name}'
output foundryOpenAiEndpoint string = 'https://${foundry.name}.openai.azure.com'
output foundryDeploymentName string = foundryModel.name
output foundryAgentIdentityClientId string = foundryAgentIdentity.properties.clientId
