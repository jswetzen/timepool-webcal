FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Set working directory
WORKDIR /app

# Copy project files
COPY README.md pyproject.toml uv.lock /app/
COPY src/ /app/src/

# Install dependencies
RUN uv sync --frozen --no-dev

# Create data directory
RUN mkdir -p /app/data

# Expose port
EXPOSE 8000

# Run the application
CMD ["uv", "run", "python", "src/timepool_webcal/timecare_webcal.py"]
