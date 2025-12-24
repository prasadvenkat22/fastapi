# MongoDB JSON Schema validators and createIndex commands

## Users validator (example)
```js
// Apply in Mongo shell or Atlas collection validator
db.runCommand({
  collMod: "users",
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["email","createdAt"],
      properties: {
        email: { bsonType: "string", pattern: "^.+@.+\\..+$" },
        createdAt: { bsonType: "date" },
        passwordHash: { bsonType: "string" }
      }
    }
  },
  validationLevel: "moderate"
})
```

## Customers validator
```js
db.runCommand({
  collMod: "customers",
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["name","createdAt"],
      properties: {
        name: { bsonType: "string" },
        status: { enum: ["active","inactive"] },
        primaryContact: { bsonType: "object" },
        tenantId: { bsonType: "string" },
        createdAt: { bsonType: "date" },
        updatedAt: { bsonType: "date" }
      }
    }
  },
  validationLevel: "moderate"
})
```

## Devices validator
```js
db.runCommand({
  collMod: "devices",
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["serialNumber","customerId","createdAt"],
      properties: {
        serialNumber: { bsonType: "string" },
        customerId: { bsonType: "objectId" },
        deviceType: { bsonType: "string" },
        firmwareVersion: { bsonType: "string" },
        lastSeenAt: { bsonType: "date" }
      }
    }
  },
  validationLevel: "moderate"
})
```

## Demo registrations validator
```js
db.runCommand({
  collMod: "registrations",
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["useremail","createdate"],
      properties: {
        useremail: { bsonType: "string", pattern: "^.+@.+\\..+$" },
        demodate: { bsonType: "date" },
        status: { enum: ["requested","scheduled","completed","cancelled"] }
      }
    }
  },
  validationLevel: "moderate"
})
```

## Recommended indexes (run in mongo shell or Atlas UI)
```js
// Users: unique email (consider per-tenant compound key: {tenantId:1, email:1})
db.users.createIndex({ email: 1 }, { unique: true })

// Customers: name lookup
db.customers.createIndex({ name: 1 })

// Devices: unique serial number
db.devices.createIndex({ serialNumber: 1 }, { unique: true })

// Devices: lookup by customerId
db.devices.createIndex({ customerId: 1 })

// Demo registrations: scheduled searches
db.registrations.createIndex({ scheduledSlot: 1, status: 1 })

// TTL for sessions or tokens (example)
db.sessions.createIndex({ expiresAt: 1 }, { expireAfterSeconds: 0 })
```

## Notes
- Apply validators in a maintenance window; use `validationLevel: "moderate"` initially to avoid rejecting legacy docs.
- For large collections consider hashed shard keys (e.g., hashed `_id` or `customerId`).
- Use Atlas Search for full-text searches rather than text indexes for production-grade search.
