# Public AWS trust anchors

`aws-trust-ca.pem` contains Amazon Root CA 1 through 4 from the official
Amazon Trust Services repository:

- https://www.amazontrust.com/repository/AmazonRootCA1.pem
- https://www.amazontrust.com/repository/AmazonRootCA2.pem
- https://www.amazontrust.com/repository/AmazonRootCA3.pem
- https://www.amazontrust.com/repository/AmazonRootCA4.pem

These are public certificates, not credentials or private keys. The Aurora
Express internet gateway uses AWS root certificates. The IAM PostgreSQL
connector verifies both the server certificate chain and endpoint hostname;
certificate verification is never disabled.
