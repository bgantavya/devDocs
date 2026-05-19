// Package store persists carts.
package store

import (
	"context"
	db "github.com/acme/db"
)
import "fmt"

const (
	MaxItems = 100
	minItems = 1
)

// Store holds carts.
type Store struct {
	conn *db.Conn
}

// Reader reads carts.
type Reader interface {
	Read(ctx context.Context, id string) (*Cart, error)
	close()
}

type ID string

// Get returns a cart.
func (s *Store) Get(ctx context.Context, id ID) (*Cart, error) {
	return nil, fmt.Errorf(`raw {`)
}

func (s Store) flush() {}

// New builds a Store.
func New[T any](conn *db.Conn) *Store {
	return &Store{conn: conn}
}
