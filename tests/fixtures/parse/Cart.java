package com.acme.cart;

import java.util.List;
import static java.util.Collections.*;

/**
 * A cart of items.
 * @author someone
 */
@Entity
public class Cart extends Base implements Priced, Serializable {
    public static final int MAX_ITEMS = 100;
    private List<Item> items;

    public Cart(List<Item> items) {
        this.items = items;
    }

    /** Total price. */
    @Override
    public BigDecimal total() {
        return items.stream().map(Item::price).reduce(BigDecimal.ZERO, BigDecimal::add);
    }

    private <T> List<T> map(Function<Item, T> f) { return null; }

    String note = """
        public void fake() {
        """;
}

interface Priced {
    BigDecimal total();
}
