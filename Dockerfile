# Builder stage
FROM rust:1.88-slim-bookworm as builder

WORKDIR /app

# Copy Cargo configuration files
COPY Cargo.toml Cargo.lock ./

# Create dummy main.rs to pre-build dependencies for caching
RUN mkdir src && echo "fn main() {}" > src/main.rs
RUN cargo build --release

# Copy the actual source files
COPY src ./src

# Rebuild with actual source files
RUN touch src/main.rs && cargo build --release

# Runtime stage
FROM debian:bookworm-slim

WORKDIR /app

# Install runtime dependencies (ca-certificates for HTTPS/SSL and sqlite3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled binary from builder stage
COPY --from=builder /app/target/release/career-scout-agent /app/career-scout-agent

# Create directories for data and logs
RUN mkdir -p /app/data /app/logs

# Set container entrypoint
CMD ["/app/career-scout-agent"]
